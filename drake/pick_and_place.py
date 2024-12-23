import numpy as np
from pydrake.all import (
    DiagramBuilder,
    InverseKinematics,
    MeshcatVisualizer,
    MeshcatVisualizerParams,
    RigidTransform,
    RotationMatrix,
    Solve,
    StartMeshcat,
    Simulator
)
from manipulation.station import LoadScenario, MakeHardwareStation
from manipulation.scenarios import AddMultibodyTriad
from manipulation.meshcat_utils import AddMeshcatTriad
import time
from pydrake.trajectories import PiecewisePolynomial
from pydrake.systems.primitives import TrajectorySource, MatrixGain
from pydrake.systems.framework import LeafSystem, BasicVector
from scipy.optimize import minimize

import os
import shutil
import subprocess
import yaml

# Start the visualizer
meshcat = StartMeshcat()
print("Meshcat URL:", meshcat.web_url())

def build_sdfs(from_scratch=False):
    """
    Build SDF files and a final scenario YAML configuration.

    Args:
        from_scratch (bool): If True, deletes and recreates the scenarios directory.
    """
    # Paths
    base_dir = "/workspace/drake/data"
    obj_dir = os.path.join(base_dir, "obj")
    sdf_dir = obj_dir
    final_yaml_path = os.path.join(base_dir, "final_scenario_draft.yaml")

    # Remove and recreate scenarios directory if from_scratch is True
    if from_scratch:
        if os.path.exists(sdf_dir):
            print(f"Resetting sdfs and final scenario.")
            shutil.rmtree(sdf_dir)
            os.remove(final_yaml_path)

    os.makedirs(sdf_dir, exist_ok=True)
    
    # Iterate through files in obj directory and generate SDFs
    scenemesh_sdfs = []
    for obj_file in os.listdir(obj_dir):
        if obj_file.endswith(".obj"):
            obj_path = os.path.join(obj_dir, obj_file)
            print(f"Processing: {obj_path}")
            try:
                subprocess.run(
                    ["python", "obj2sdf.py", "--mass", "10", "--mesh", obj_path],
                    check=True
                )
                sdf_path = os.path.join(sdf_dir, f"{os.path.splitext(obj_file)[0]}.sdf")
                scenemesh_sdfs.append(sdf_path)
            except subprocess.CalledProcessError as e:
                print(f"Error generating SDF for {obj_file}: {e}")

    # Create final_scenario.yaml with directives
    directives = [
        # Add other models as required
        {
            "add_model": {
                "name": "iiwa",
                "file": "package://drake_models/iiwa_description/sdf/iiwa7_no_collision.sdf",
                "default_joint_positions": {
                    "iiwa_joint_1": [-1.57],
                    "iiwa_joint_2": [0.1],
                    "iiwa_joint_3": [0],
                    "iiwa_joint_4": [-1.2],
                    "iiwa_joint_5": [0],
                    "iiwa_joint_6": [1.6],
                    "iiwa_joint_7": [0]
                }
            }
        },
        {"add_weld": {
            "parent": "world",
            "child": "iiwa::iiwa_link_0",
            "X_PC": {
                "translation": [0, 0, 0],
                "rotation": {"!Rpy": {"deg": [0, 0, 180]}}
            }
        }},
        {"add_model": {
            "name": "wsg",
            "file": "package://manipulation/hydro/schunk_wsg_50_with_tip.sdf"
        }},
        {"add_weld": {
            "parent": "iiwa::iiwa_link_7",
            "child": "wsg::body",
            "X_PC": {
                "translation": [0, 0, 0.09],
                "rotation": {"!Rpy": {"deg": [0, 0, 180]}}
            }
        }},
    ]

    directives.append({
        "add_model:" : {
            "name": "table",
            "file": "file:///workspace/drake/table.sdf",
            "default_free_body_pose": {
                "link": {
                    "translation": [0, 0, 0],
                    "rotation": {"!Rpy": {"deg": [0, 0, 0]}}
                }
            }
        }
    })

    # Add scenemesh SDFs
    for sdf_path in scenemesh_sdfs:
        directives.append({
            "add_model": {
                "name": f"{os.path.basename(sdf_path).split('.')[0]}",
                "file": f"file://{sdf_path}",
                "default_free_body_pose": {
                    f"{os.path.basename(sdf_path).split('.')[0]}_body_link": {
                        "translation": [0.0, 0.0, 0.0],
                        "rotation": {"!Rpy": {"deg": [0, 0, 180]}}
                    }
                }
            }
        })

    yaml.Dumper.add_representer(
        dict,
        lambda dumper, data: dumper.represent_mapping(
            "tag:yaml.org,2002:map", data, flow_style=None
        )
    )

    # Save directives to YAML file
    print(f"Writing final scenario YAML to: {final_yaml_path}")
    with open(final_yaml_path, 'w') as yaml_file:
        yaml.dump({"directives": directives}, yaml_file, default_flow_style=False)

    return final_yaml_path

def build_env():
    """Build the simulation environment and set up visualization with Meshcat."""
    builder = DiagramBuilder()
    scenario = LoadScenario(filename=final_scenario_path)
    station = builder.AddSystem(MakeHardwareStation(scenario, meshcat))
    plant = station.GetSubsystemByName("plant")
    scene_graph = station.GetSubsystemByName("scene_graph")

    MeshcatVisualizer.AddToBuilder(
        builder,
        station.GetOutputPort("query_object"),
        meshcat,
        MeshcatVisualizerParams(delete_on_initialization_event=False),
    )
    AddMultibodyTriad(plant.GetFrameByName("body"), scene_graph)
    diagram = builder.Build()
    context = plant.CreateDefaultContext()
    initial_q = plant.GetPositions(context)
    return diagram, plant, scene_graph, initial_q


def solve_ik(X_WG, max_tries=100, initial_guess=None):
    """Solve the inverse kinematics problem for the given goal pose, including orientation constraints."""
    diagram, plant, scene_graph, initial_q = build_env()
    context = diagram.CreateDefaultContext()
    plant_context = plant.GetMyContextFromRoot(context)

    ik = InverseKinematics(plant, plant_context)
    q_variables = ik.q()
    prog = ik.prog()

    q_nominal = initial_guess if initial_guess is not None else np.zeros(len(q_variables))
    prog.AddQuadraticErrorCost(np.eye(len(q_variables)), q_nominal, q_variables)

    # Add position constraint
    ik.AddPositionConstraint(
        frameB=plant.GetFrameByName("body"),
        p_BQ=np.array([0, 0, 0]),
        frameA=plant.world_frame(),
        p_AQ_lower=X_WG.translation() - 0.01,
        p_AQ_upper=X_WG.translation() + 0.01,
    )

    # Add orientation constraint
    ik.AddOrientationConstraint(
        frameAbar=plant.world_frame(),
        R_AbarA=X_WG.rotation(),
        frameBbar=plant.GetFrameByName("body"),
        R_BbarB=RotationMatrix.Identity(),
        theta_bound=0.1,
    )

    for count in range(max_tries):
        q_initial_guess = (
            initial_guess if initial_guess is not None else np.random.uniform(-1.0, 1.0, len(q_variables))
        )
        prog.SetInitialGuess(q_variables, q_initial_guess)
        result = Solve(prog)

        if result.is_success():
            print(f"Succeeded in {count + 1} tries.")
            return diagram, plant, scene_graph, initial_q, result.GetSolution(q_variables), context

    print("IK failed!")
    return None, None, None, None, None, None


class GripperControlSystem(LeafSystem):
    """Control system for the gripper."""
    def __init__(self, open_position=0.25, close_position=0.0, close_time=10.0, open_time=20.0):
        super().__init__()
        self.open_position = open_position
        self.close_position = close_position
        self.close_time = close_time
        self.open_time = open_time
        self.DeclareVectorOutputPort("wsg.position", BasicVector(1), self.CalcGripperPosition)

    def CalcGripperPosition(self, context, output):
        current_time = context.get_time()
        if current_time >= self.open_time:
            output.SetAtIndex(0, self.open_position)
        elif current_time >= self.close_time:
            output.SetAtIndex(0, self.close_position)
        else:
            output.SetAtIndex(0, self.open_position)


def visualize_goal_frames(meshcat, goal_poses):
    """Visualize multiple goal poses in Meshcat."""
    for idx, pose in enumerate(goal_poses, start=1):
        AddMeshcatTriad(
            meshcat,
            path=f"goal_frame_{idx}",
            X_PT=pose,
            length=0.2,
            radius=0.01,
            opacity=1.0,
        )

def generate_trajectory(initial_q, final_q, num_steps=100, weight_smoothness=10.0, weight_straightness=5000.0):
    """
    Generate a trajectory that minimizes deviation from a straight line between initial and final configurations.
    """
    n_dofs = len(initial_q)
    alphas = np.linspace(0, 1, num_steps)
    straight_line = np.array([(1 - alpha) * initial_q + alpha * final_q for alpha in alphas])

    def trajectory_cost(traj):
        traj = traj.reshape(num_steps, n_dofs)
        # Smoothness cost: penalize changes between consecutive points
        smoothness_cost = sum(np.linalg.norm(traj[i] - traj[i - 1]) ** 2 for i in range(1, num_steps))
        # Straightness cost: penalize deviation from straight line
        straightness_cost = sum(np.linalg.norm(traj[i] - straight_line[i]) ** 2 for i in range(num_steps))
        return weight_smoothness * smoothness_cost + weight_straightness * straightness_cost

    # Minimize the cost function
    result = minimize(
        trajectory_cost,
        straight_line.flatten(),  # Use straight line as initial guess
        method="L-BFGS-B",
        options={"disp": True}
    )

    # Reshape the result into the trajectory
    optimized_trajectory = result.x.reshape(num_steps, n_dofs)
    return optimized_trajectory

from scipy.optimize import minimize, Bounds

def generate_restricted_trajectory(initial_q, final_q, num_steps=100, weight_smoothness=10.0, weight_straightness=100.0, tolerance=0.02):
    """
    Generate a trajectory with explicit constraints for linear interpolation and smoothness.
    """
    n_dofs = len(initial_q)
    alphas = np.linspace(0, 1, num_steps)
    straight_line = np.array([(1 - alpha) * initial_q + alpha * final_q for alpha in alphas])

    def trajectory_cost(traj):
        traj = traj.reshape(num_steps, n_dofs)
        # Smoothness cost: penalize large changes between consecutive points
        smoothness_cost = sum(np.linalg.norm(traj[i] - traj[i - 1]) ** 2 for i in range(1, num_steps))
        # Straightness cost: penalize deviation from the straight line
        straightness_cost = sum(np.linalg.norm(traj[i] - straight_line[i]) ** 2 for i in range(num_steps))
        return weight_smoothness * smoothness_cost + weight_straightness * straightness_cost

    # Bounds: Ensure trajectory points are within tolerance of the straight line
    lower_bounds = (straight_line - tolerance).flatten()
    upper_bounds = (straight_line + tolerance).flatten()
    bounds = Bounds(lower_bounds, upper_bounds)

    # Minimize the cost function
    result = minimize(
        trajectory_cost,
        straight_line.flatten(),  # Use straight line as initial guess
        method="L-BFGS-B",
        bounds=bounds,
        options={"disp": True}
    )

    # Reshape the result into the trajectory
    optimized_trajectory = result.x.reshape(num_steps, n_dofs)
    return optimized_trajectory

def build_and_simulate_trajectory(q_traj, gripper_close_time, gripper_open_time):
    """Simulate the robot's motion along the generated trajectory."""
    builder = DiagramBuilder()
    scenario = LoadScenario(filename=final_scenario_path)
    station = builder.AddSystem(MakeHardwareStation(scenario, meshcat))

    # Add trajectory source
    q_traj_system = builder.AddSystem(TrajectorySource(q_traj))
    trim_gain = builder.AddSystem(MatrixGain(np.eye(7, 16)))
    builder.Connect(q_traj_system.get_output_port(), trim_gain.get_input_port())
    builder.Connect(trim_gain.get_output_port(), station.GetInputPort("iiwa.position"))

    # Add gripper control
    gripper_control_system = builder.AddSystem(
        GripperControlSystem(
            open_position=0.2,
            close_position=0.0,
            close_time=gripper_close_time,
            open_time=gripper_open_time
        )
    )
    builder.Connect(gripper_control_system.get_output_port(0), station.GetInputPort("wsg.position"))

    diagram = builder.Build()
    simulator = Simulator(diagram)
    simulator.set_target_realtime_rate(1.0)

    # Simulate
    meshcat.StartRecording(set_visualizations_while_recording=False)
    simulator.AdvanceTo(gripper_open_time + 1.0)
    meshcat.PublishRecording()
    print("Simulation completed.")

def generate_combined_trajectory(pose_nodes, num_steps=100, segment_duration=5):
    trajectories = []
    time_segments = []
    segment_times = []

    # generate trajectories between consecutive pose_nodes
    for i in range(len(pose_nodes) - 1):
        trajectory = generate_restricted_trajectory(pose_nodes[i], pose_nodes[i + 1], num_steps)
        trajectories.append(np.array(trajectory).T)

        start_time = i * segment_duration
        end_time = (i + 1) * segment_duration
        print(start_time, end_time)

        time_segment = np.linspace(start_time, end_time, num_steps)

        if i > 0:
            time_segment = time_segment[1:]
        
        time_segments.append(time_segment)
        segment_times.append((start_time, end_time))

    # combine time segments and trajectories
    combined_times = np.hstack(time_segments)
    combined_trajectory = np.hstack([trajectory[:, 1:] if i > 0 else trajectory for i, trajectory in enumerate(trajectories)])

    return combined_times, combined_trajectory, segment_times


def extract_target_goal_pose():
    directory = os.path.join(os.getcwd(), "grasp_results/mustard")
    goal_pose_path = os.path.join(directory, "mustard_w_post_processing_scaled.npy")
    goal_pose = np.load(goal_pose_path)

    # top pose
    goal_pose = goal_pose[0]
    rot = np.array(goal_pose[:3, :3])
    trans = np.array(goal_pose[:3, 3]) + [0.275, 0.275, 0.4] # offset on the table (from scenario yaml)
    
    # shift to account for gripper block
    trans += rot @ [0.0205, -0.1, 0]

    return RigidTransform(RotationMatrix(rot), trans)

if __name__ == "__main__":

    # Finalized scenario
    # _ = build_sdfs()

    final_scenario_path = os.path.join(os.getcwd(), "data/final_scenario.yaml")
    print(final_scenario_path)

    # Define goal poses
    goal_pose_target = extract_target_goal_pose()
    desired_rotation = goal_pose_target.rotation()
    target_translation = goal_pose_target.translation()

    # goal_pose_target = RigidTransform(goal_rotation, np.array([-0.04, 0.65, 0.53]))

    goal_poses = []
    # append target
    goal_poses.append(goal_pose_target)
    # above target (in the z-axis)
    goal_poses.append(
        RigidTransform(desired_rotation, 
                       np.array([target_translation[0], target_translation[1], 0]) + np.array([0, 0, 0.6])))
    # append translated final pose (in the z-axis)
    goal_poses.append(
        RigidTransform(desired_rotation, 
                       np.array([-0.2, 0.4, 0.6]))
    )
    # append final pose
    goal_poses.append(
        RigidTransform(desired_rotation, 
                       np.array([goal_poses[-1].translation()[0],
                                 goal_poses[-1].translation()[1],
                                 target_translation[2]]))
    )

    # prepend initial pose (close to target in the y-axis)
    goal_poses.insert(0,
        RigidTransform(desired_rotation, 
                       target_translation + desired_rotation @ np.array([0, -0.1, 0]).T))

    # Visualize goal frames
    visualize_goal_frames(meshcat, goal_poses)

    initial_guess = None
    pose_nodes = []    

    for ik_num in range(len(goal_poses)):
        print(f"Solve IK {ik_num}...")
        _, _, _, initial_guess, res, _ = solve_ik(goal_poses[ik_num], initial_guess=initial_guess)
        if res is None:
            exit(code=1)
        if ik_num == 0:
            pose_nodes.append(initial_guess)
        pose_nodes.append(res)
        initial_guess = res

    # Solve IK for all poses
    combined_times, combined_trajectory, segment_times = generate_combined_trajectory(pose_nodes)

    q_traj_combined = PiecewisePolynomial.FirstOrderHold(combined_times, combined_trajectory)

    # Simulate
    print(q_traj_combined)

    build_and_simulate_trajectory(q_traj_combined,
                                  gripper_close_time=segment_times[2][0], # target goal pose
                                  gripper_open_time=segment_times[-1][1]) # end goal pose

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Visualization interrupted.")

