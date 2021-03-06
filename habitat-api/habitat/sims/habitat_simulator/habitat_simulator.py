#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import os.path
from os import stat
import cv2.cv2
import torch
from typing import Any, List, Optional, Union

import numpy as np
from gym import spaces

import habitat_sim

import numpy as np
import scipy.ndimage as nd
from matplotlib.transforms import Affine2D

from mapper.map import convert_midlevel_to_map
from mapper.mid_level.decoder import UpResNet
from mapper.mid_level.encoder import mid_level_representations
from mapper.mid_level.fc import FC
from mapper.transform import egomotion_transform
from mapper.update import update_map

try:
    import cupy
    import cupyx.scipy.ndimage as ndc
    CUPYAVAILABLE = True
    print('Using cupyx')
except ImportError:
    print("cuda not enabled for affine transforms")
    CUPYAVAILABLE = False

import habitat
from config.config import MAP_DIMENSIONS, MAP_SIZE, MAP_DOWNSAMPLE, DATASET_SAVE_PERIOD, DATASET_SAVE_FOLDER, \
    START_IMAGE_NUMBER, MID_LEVEL_DIMENSIONS, DEBUG, REPRESENTATION_NAMES, device, RESIDUAL_LAYERS_PER_BLOCK, \
    RESIDUAL_NEURON_CHANNEL, RESIDUAL_SIZE, STRIDES, BATCHSIZE
from habitat.core.dataset import Episode
from habitat.core.logging import logger
from habitat.core.registry import registry
from habitat.core.simulator import (
    AgentState,
    Config,
    DepthSensor,
    Observations,
    RGBSensor,
    SemanticSensor,
    Sensor,
    SensorSuite,
    ShortestPathPoint,
    Simulator,
    SensorTypes,
)
from habitat.core.spaces import Space
from habitat.utils import profiling_utils
from habitat.utils.visualizations import fog_of_war, maps

import matplotlib.pyplot as plt

from habitat.utils.visualizations.maps import quat_to_angle_axis

RGBSENSOR_DIMENSION = 3


def overwrite_config(config_from: Config, config_to: Any) -> None:
    r"""Takes Habitat-API config and Habitat-Sim config structures. Overwrites
    Habitat-Sim config with Habitat-API values, where a field name is present
    in lowercase. Mostly used to avoid :ref:`sim_cfg.field = hapi_cfg.FIELD`
    code.

    Args:
        config_from: Habitat-API config node.
        config_to: Habitat-Sim config structure.
    """

    def if_config_to_lower(config):
        if isinstance(config, Config):
            return {key.lower(): val for key, val in config.items()}
        else:
            return config

    for attr, value in config_from.items():
        if hasattr(config_to, attr.lower()):
            setattr(config_to, attr.lower(), if_config_to_lower(value))


def check_sim_obs(obs, sensor):
    assert obs is not None, (
        "Observation corresponding to {} not present in "
        "simulator's observations".format(sensor.uuid)
    )


@registry.register_sensor
class HabitatSimRGBSensor(RGBSensor):
    sim_sensor_type: habitat_sim.SensorType

    def __init__(self, sim, config):
        self._sim = sim
        self.sim_sensor_type = habitat_sim.SensorType.COLOR
        super().__init__(config=config)
        self.image_number = 0
        self.prev_pose = None

    def _get_observation_space(self, *args: Any, **kwargs: Any):
        return spaces.Box(
            low=0,
            high=255,
            shape=(self.config.HEIGHT, self.config.WIDTH, RGBSENSOR_DIMENSION),
            dtype=np.uint8,
        )

    def get_observation(self, sim_obs):
        obs = sim_obs.get(self.uuid, None)
        check_sim_obs(obs, self)

        # remove alpha channel
        obs = obs[:, :, :RGBSENSOR_DIMENSION]

        # if self.image_number % DATASET_SAVE_PERIOD == 0:
            # print('Saving RGB image: ', self.image_number)
            # plt.imsave(os.path.join(DATASET_SAVE_FOLDER, 'images', f'rgb_{self.current_scene_name}_{str((self.image_number // DATASET_SAVE_PERIOD) + START_IMAGE_NUMBER)}.jpeg'), obs)

        self.image_number = self.image_number + 1
        return obs


@registry.register_sensor(name='MIDLEVEL')
class HabitatSimMidLevelSensor(Sensor):
    """ Holds mid level encodings """

    sim_sensor_type: habitat_sim.SensorType

    def __init__(self, sim, config):
        self._sim = sim
        self.sim_sensor_type = habitat_sim.SensorType.NONE
        super().__init__(config=config)

    def _get_uuid(self, *args: Any, **kwargs: Any) -> str:
        return 'midlevel'

    def _get_sensor_type(self, *args: Any, **kwargs: Any) -> SensorTypes:
        return self.sim_sensor_type

    def _get_observation_space(self, *args: Any, **kwargs: Any):
        return spaces.Box(
            low=0,
            high=255,
            shape=MID_LEVEL_DIMENSIONS,
            dtype=np.uint8,
        )

    def get_observation(self, sim_obs):
        obs = sim_obs.get('rgb', None)
        check_sim_obs(obs, self)

        # remove alpha channel
        obs = obs[:, :, :RGBSENSOR_DIMENSION]

        obs = torch.Tensor(obs)
        obs = obs.to(device)
        obs = torch.transpose(obs, 0, 2)
        obs = obs.unsqueeze(0)

        if DEBUG:
            print(f"Encoding image of shape {obs.shape} with mid level encoders.")
        obs = mid_level_representations(obs, REPRESENTATION_NAMES)
        if DEBUG:
            print(f'Returning encoded representation of shape {obs.shape}.')
        sim_obs['midlevel'] = obs
        obs = obs[0, :, :, :]
        return obs


@registry.register_sensor(name="EGOMOTION")
class AgentPositionSensor(Sensor):
    def __init__(self, sim, config):
        self.sim_sensor_type = habitat_sim.SensorType.NONE
        super().__init__(config=config)
        self._sim = sim
        self.prev_pose = None

    # Defines the name of the sensor in the sensor suite dictionary
    def _get_uuid(self, *args, **kwargs):
        return "egomotion"

    # Defines the type of the sensor
    def _get_sensor_type(self, *args, **kwargs):
        return self.sim_sensor_type

    # Defines the size and range of the observations of the sensor
    def _get_observation_space(self, *args, **kwargs):
        return spaces.Box(
            low=np.finfo(np.float32).min,
            high=np.finfo(np.float32).max,
            shape=(1,1,3),
            dtype=np.float32,
        )

    # This is called whenver reset is called or an action is taken
    def get_observation(self, sim_obs) -> Any:
        pos = (self._sim.get_agent_state().position[0],self._sim.get_agent_state().position[2])
        sim_quat = self._sim.get_agent_state().rotation
        alpha = -quat_to_angle_axis(sim_quat)[0] + np.pi/2

        state = np.array([pos[0],pos[1],alpha])

        if self.prev_pose is None:
            self.prev_pose = state
            initial_displacement = torch.Tensor(np.zeros((1, 1, 3)))
            initial_displacement = initial_displacement.to(device)
            sim_obs['egomotion'] = initial_displacement
            return initial_displacement

        world_displacement = state - self.prev_pose  # displacement in the world frame
        world_to_robot_transformation_matrix = Affine2D().rotate_around(0, 0, np.pi/2-self.prev_pose[2]).get_matrix()  # negative rotation to compensate for positive rotation
        robot_displacement = torch.Tensor(world_to_robot_transformation_matrix @ world_displacement)
        robot_displacement = robot_displacement.to(device)
        robot_displacement = torch.unsqueeze(robot_displacement, 0)
        robot_displacement = torch.unsqueeze(robot_displacement, 0)
        self.prev_pose = state
        sim_obs['egomotion'] = robot_displacement
        return robot_displacement


@registry.register_sensor(name='MIDLEVEL_MAP_SENSOR')
class HabitatSimMidLevelMapSensor(Sensor):
    """ Holds the map generated from mid level representations. """

    sim_sensor_type: habitat_sim.SensorType

    def __init__(self, sim, config):
        self._sim = sim
        self.sim_sensor_type = habitat_sim.SensorType.NONE
        super().__init__(config=config)
        # zero confidence, so this is not taken into account in first map update.
        self.previous_map = torch.zeros((BATCHSIZE, *MAP_DIMENSIONS))
        self.previous_map = self.previous_map.to(device)
        # self.previous_map.requires_grad_(True)
        self.fc = FC()
        self.fc.to(device)
        self.upresnet = UpResNet(
            layers=RESIDUAL_LAYERS_PER_BLOCK,
            channels=RESIDUAL_NEURON_CHANNEL,
            sizes=RESIDUAL_SIZE,
            strides=STRIDES
        )
        self.upresnet.to(device)

    def _get_uuid(self, *args: Any, **kwargs: Any) -> str:
        return 'midlevel_map'

    def _get_sensor_type(self, *args: Any, **kwargs: Any) -> SensorTypes:
        return self.sim_sensor_type

    def _get_observation_space(self, *args: Any, **kwargs: Any):
        return spaces.Box(
            low=0,
            high=1,
            shape=MAP_DIMENSIONS,
            dtype=np.float32,
        )

    def get_observation(self, sim_obs):
        # return previous map for policy, but ensure to calculate the new map for the next update
        return_value = self.previous_map.clone()
        midlevel_obs = sim_obs["midlevel"]
        egomotion_obs = sim_obs["egomotion"]
        decoded_map = convert_midlevel_to_map(midlevel_obs, self.fc, self.upresnet)
        dx = egomotion_obs
        previous_map = egomotion_transform(self.previous_map, dx)
        with torch.no_grad():
            new_map = update_map(decoded_map, previous_map)
            self.previous_map = new_map
        return return_value[0, :, :, :]


@registry.register_sensor(name='MAP_SENSOR')
class HabitatSimMapSensor(Sensor):
    sim_sensor_type: habitat_sim.SensorType
    """
        Custom class to create a map sensor.
    """

    def __init__(self, sim, config):
        # self.sim_sensor_type = habitat_sim.SensorType.TENSOR ----> TENSOR DOESN'T EXIST IN 2019 TENSORFLOW :(
        self.sim_sensor_type = habitat_sim.SensorType.COLOR
        super().__init__(config=config)
        self._sim = sim
        self.image_number = 0
        self.cone = self.vis_cone((MAP_DIMENSIONS[1], MAP_DIMENSIONS[2]), np.pi/1.1)
        self.map_scale_factor = 4
        self.map_upsample_factor = 2
        self.global_map = None
        self.origin = None
        self.displacements = []

    # Defines the name of the sensor in the sensor suite dictionary
    def _get_uuid(self, *args: Any, **kwargs: Any) -> str:
        return 'map'

    # Defines the type of the sensor
    def _get_sensor_type(self, *args: Any, **kwargs: Any) -> SensorTypes:
        return self.sim_sensor_type

    # Defines the size and range of the observations of the sensor
    def _get_observation_space(self, *args: Any, **kwargs: Any) -> Space:
        return spaces.Box(
            low=0,
            high=1,
            shape=(MAP_DIMENSIONS[1], MAP_DIMENSIONS[2], MAP_DIMENSIONS[0]),
            dtype=np.uint8,
        )

    def vis_cone(self, map_size, fov):
        cone = np.zeros(map_size)

        ci = np.floor(map_size[0]/2)
        cj = np.floor(map_size[1]/2)
        for ii in range(map_size[0]):
            for jj in range(map_size[1]):
                di = ii - ci
                dj = jj - cj
                angle = np.arctan2(dj, -di)
                if((- fov/2)<angle<fov/2):
                    cone[ii,jj] = 1
                else:
                    cone[ii,jj] = 0

        return cone

    def compute_global_map(self):
        self.global_map = maps.get_topdown_map_sensor( # this is kinda not great, ideally we should only compute a map on reset and just reuse the same map file every step (differently translated)
                sim=self._sim,
                map_resolution=(MAP_DIMENSIONS[1] * self.map_scale_factor // self.map_upsample_factor, MAP_DIMENSIONS[2] * self.map_scale_factor // self.map_upsample_factor),
                map_size=(MAP_SIZE[0]* self.map_scale_factor, MAP_SIZE[1]* self.map_scale_factor),
            )
        self.global_map = cv2.cv2.resize(self.global_map, (MAP_DIMENSIONS[1] * self.map_scale_factor, MAP_DIMENSIONS[2] * self.map_scale_factor))
        # Compute origin

    # This is called whenever reset is called or an action is taken
    def get_observation(self, _) -> Any:

        pos = (self._sim.get_agent_state().position[0],self._sim.get_agent_state().position[2])
        sim_quat = self._sim.get_agent_state().rotation
        alpha = -quat_to_angle_axis(sim_quat)[0] + np.pi/2

        if self.global_map is None:
            self.compute_global_map()
            self.origin = np.array([pos[0], pos[1], alpha])
            # plt.imsave('debug/global_map' + '.jpeg', self.global_map)

        state = np.array([pos[0],pos[1],alpha])

        world_displacement = state - self.origin  # displacement in the world frame

        world_to_map_transformation_matrix = Affine2D().rotate_around(0, 0, np.pi/2-self.origin[2]).get_matrix()  # negative rotation to compensate for positive rotation
        map_displacement = world_to_map_transformation_matrix @ world_displacement

        di = np.floor(map_displacement[0] * (MAP_DIMENSIONS[1]/MAP_SIZE[0]))
        dj = np.floor(map_displacement[1] * (MAP_DIMENSIONS[2]/MAP_SIZE[1]))

        width = self.global_map.shape[0]
        height = self.global_map.shape[1]
        T = (Affine2D().rotate_around(width//2,height//2,-map_displacement[2]) + Affine2D().translate(tx=dj, ty=di)).get_matrix()

        global_map_copy = np.copy(self.global_map)

        if CUPYAVAILABLE:
            output_map = cupy.asnumpy(ndc.affine_transform(cupy.asarray(global_map_copy), cupy.asarray(T)))
        else:
            output_map = nd.affine_transform(global_map_copy, T)

        cy = height // 2
        cx = width // 2

        circle_map = cv2.merge((global_map_copy*128,global_map_copy*128,global_map_copy*128))
        circle_map = cv2.circle(circle_map, (int(di+cx),int(dj+cy)), 3, (255,0,0), 2)

        output_map = output_map[cx-width//(2*self.map_scale_factor):cx+width//(2*self.map_scale_factor),\
                                cy-width//(2*self.map_scale_factor):cy+height//(2*self.map_scale_factor)]

        output_map = self.cone * output_map

        if self.image_number % DATASET_SAVE_PERIOD == 0:
            self.displacements.append(np.concatenate((np.array([self.image_number]), map_displacement, np.array([di, dj]))))
            if self.image_number == 1200:
                with open('data/nuevo_displacements.npy', 'wb') as f:
                    np.save(f, np.array(self.displacements))
            # plt.imsave(os.path.join(DATASET_SAVE_FOLDER, 'maps', f'map_{self.current_scene_name}_{str((self.image_number // DATASET_SAVE_PERIOD) + START_IMAGE_NUMBER)}.jpeg'), output_map)
            # plt.imsave(os.path.join(DATASET_SAVE_FOLDER, 'circle_maps', f'circle_map_{self.current_scene_name}_{str((self.image_number // DATASET_SAVE_PERIOD) + START_IMAGE_NUMBER)}.jpeg'), circle_map)

        output_map = torch.unsqueeze(torch.from_numpy(output_map),0).to(torch.float32)
        confmap = torch.unsqueeze(torch.from_numpy(self.cone),0).to(torch.float32)
        output_map = torch.cat((output_map,confmap), dim=0)
        output_map = output_map.permute(1, 2, 0)

        # Assert we have only map and confidence channels
        assert output_map.shape[2] == MAP_DIMENSIONS[0]

        self.image_number = self.image_number + 1

        return output_map


@registry.register_simulator(name="Sim-v0")
class HabitatSim(habitat_sim.Simulator, Simulator):
    r"""Simulator wrapper over habitat-sim

    habitat-sim repo: https://github.com/facebookresearch/habitat-sim

    Args:
        config: configuration for initializing the simulator.
    """

    def __init__(self, config: Config) -> None:
        self.habitat_config = config
        agent_config = self._get_agent_config()

        sim_sensors = []
        for sensor_name in agent_config.SENSORS:
            sensor_cfg = getattr(self.habitat_config, sensor_name)
            sensor_type = registry.get_sensor(sensor_cfg.TYPE)

            assert sensor_type is not None, "invalid sensor type {}".format(
                sensor_cfg.TYPE
            )

            sim_sensors.append(sensor_type(self, sensor_cfg)) # we add the simulator object when initialising the sensors

        self._sensor_suite = SensorSuite(sim_sensors)
        self.sim_config = self.create_sim_config(self._sensor_suite)
        self._current_scene = self.sim_config.sim_cfg.scene.id
        super().__init__(self.sim_config)
        self._action_space = spaces.Discrete(
            len(self.sim_config.agents[0].action_space)
        )
        self._prev_sim_obs = None

    def create_sim_config(
        self, _sensor_suite: SensorSuite
    ) -> habitat_sim.Configuration:
        sim_config = habitat_sim.SimulatorConfiguration()
        overwrite_config(
            config_from=self.habitat_config.HABITAT_SIM_V0,
            config_to=sim_config,
        )
        sim_config.scene.id = self.habitat_config.SCENE
        agent_config = habitat_sim.AgentConfiguration()
        overwrite_config(
            config_from=self._get_agent_config(), config_to=agent_config
        )

        sensor_specifications = []
        for sensor in _sensor_suite.sensors.values():
            sim_sensor_cfg = habitat_sim.SensorSpec()
            overwrite_config(
                config_from=sensor.config, config_to=sim_sensor_cfg
            )
            sim_sensor_cfg.uuid = sensor.uuid
            sim_sensor_cfg.resolution = list(
                sensor.observation_space.shape[:2]
            )
            sim_sensor_cfg.parameters["hfov"] = str(sensor.config.HFOV)

            # TODO(maksymets): Add configure method to Sensor API to avoid
            # accessing child attributes through parent interface
            sim_sensor_cfg.sensor_type = sensor.sim_sensor_type  # type: ignore
            sim_sensor_cfg.gpu2gpu_transfer = (
                self.habitat_config.HABITAT_SIM_V0.GPU_GPU
            )
            sensor_specifications.append(sim_sensor_cfg)

        agent_config.sensor_specifications = sensor_specifications
        agent_config.action_space = registry.get_action_space_configuration(
            self.habitat_config.ACTION_SPACE_CONFIG
        )(self.habitat_config).get()

        return habitat_sim.Configuration(sim_config, [agent_config])

    @property
    def sensor_suite(self) -> SensorSuite:
        return self._sensor_suite

    @property
    def action_space(self) -> Space:
        return self._action_space

    def _update_agents_state(self) -> bool:
        is_updated = False
        for agent_id, _ in enumerate(self.habitat_config.AGENTS):
            agent_cfg = self._get_agent_config(agent_id)
            if agent_cfg.IS_SET_START_STATE:
                self.set_agent_state(
                    agent_cfg.START_POSITION,
                    agent_cfg.START_ROTATION,
                    agent_id,
                )
                is_updated = True

        return is_updated

    def reset(self):
        sim_obs = super().reset()
        if self._update_agents_state():
            sim_obs = self.get_sensor_observations()

        self._prev_sim_obs = sim_obs
        return self._sensor_suite.get_observations(sim_obs)

    def step(self, action):
        profiling_utils.range_push("habitat_simulator.py step")
        sim_obs = super().step(action)
        self._prev_sim_obs = sim_obs
        observations = self._sensor_suite.get_observations(sim_obs)
        profiling_utils.range_pop()  # habitat_simulator.py step
        return observations

    def render(self, mode: str = "rgb") -> Any:
        r"""
        Args:
            mode: sensor whose observation is used for returning the frame,
                eg: "rgb", "depth", "semantic"

        Returns:
            rendered frame according to the mode
        """
        sim_obs = self.get_sensor_observations()
        observations = self._sensor_suite.get_observations(sim_obs)

        output = observations.get(mode)
        assert output is not None, "mode {} sensor is not active".format(mode)
        if not isinstance(output, np.ndarray):
            # If it is not a numpy array, it is a torch tensor
            # The function expects the result to be a numpy array
            output = output.to("cpu").numpy()

        return output

    def reconfigure(self, habitat_config: Config) -> None:
        # TODO(maksymets): Switch to Habitat-Sim more efficient caching
        is_same_scene = habitat_config.SCENE == self._current_scene
        self.habitat_config = habitat_config
        self.sim_config = self.create_sim_config(self._sensor_suite)
        if not is_same_scene:
            self._current_scene = habitat_config.SCENE
            self.close()
            super().reconfigure(self.sim_config)

        self._update_agents_state()

    def geodesic_distance(
        self, position_a, position_b, episode: Optional[Episode] = None
    ):
        if episode is None or episode._shortest_path_cache is None:
            path = habitat_sim.MultiGoalShortestPath()
            if isinstance(position_b[0], List) or isinstance(
                position_b[0], np.ndarray
            ):
                path.requested_ends = np.array(position_b, dtype=np.float32)
            else:
                path.requested_ends = np.array(
                    [np.array(position_b, dtype=np.float32)]
                )
        else:
            path = episode._shortest_path_cache

        path.requested_start = np.array(position_a, dtype=np.float32)

        self.pathfinder.find_path(path)

        if episode is not None:
            episode._shortest_path_cache = path

        return path.geodesic_distance

    def action_space_shortest_path(
        self, source: AgentState, targets: List[AgentState], agent_id: int = 0
    ) -> List[ShortestPathPoint]:
        r"""
        Returns:
            List of agent states and actions along the shortest path from
            source to the nearest target (both included). If one of the
            target(s) is identical to the source, a list containing only
            one node with the identical agent state is returned. Returns
            an empty list in case none of the targets are reachable from
            the source. For the last item in the returned list the action
            will be None.
        """
        raise NotImplementedError(
            "This function is no longer implemented. Please use the greedy "
            "follower instead"
        )

    @property
    def up_vector(self):
        return np.array([0.0, 1.0, 0.0])

    @property
    def forward_vector(self):
        return -np.array([0.0, 0.0, 1.0])

    def is_navigable_path(self, position_a, position_b):
        path = habitat_sim.ShortestPath()
        path.requested_start = position_a
        path.requested_end = position_b
        return self.pathfinder.find_path(path)

    def get_straight_shortest_path_points(self, position_a, position_b):
        path = habitat_sim.ShortestPath()
        path.requested_start = position_a
        path.requested_end = position_b
        self.pathfinder.find_path(path)
        return path.points

    def sample_navigable_point(self):
        return self.pathfinder.get_random_navigable_point().tolist()

    def is_navigable(self, point: List[float]):
        return self.pathfinder.is_navigable(point)

    def semantic_annotations(self):
        r"""
        Returns:
            SemanticScene which is a three level hierarchy of semantic
            annotations for the current scene. Specifically this method
            returns a SemanticScene which contains a list of SemanticLevel's
            where each SemanticLevel contains a list of SemanticRegion's where
            each SemanticRegion contains a list of SemanticObject's.

            SemanticScene has attributes: aabb(axis-aligned bounding box) which
            has attributes aabb.center and aabb.sizes which are 3d vectors,
            categories, levels, objects, regions.

            SemanticLevel has attributes: id, aabb, objects and regions.

            SemanticRegion has attributes: id, level, aabb, category (to get
            name of category use category.name()) and objects.

            SemanticObject has attributes: id, region, aabb, obb (oriented
            bounding box) and category.

            SemanticScene contains List[SemanticLevels]
            SemanticLevel contains List[SemanticRegion]
            SemanticRegion contains List[SemanticObject]

            Example to loop through in a hierarchical fashion:
            for level in semantic_scene.levels:
                for region in level.regions:
                    for obj in region.objects:
        """
        return self.semantic_scene

    def _get_agent_config(self, agent_id: Optional[int] = None) -> Any:
        if agent_id is None:
            agent_id = self.habitat_config.DEFAULT_AGENT_ID
        agent_name = self.habitat_config.AGENTS[agent_id]
        agent_config = getattr(self.habitat_config, agent_name)
        return agent_config

    def get_agent_state(self, agent_id: int = 0) -> habitat_sim.AgentState:
        assert agent_id == 0, "No support of multi agent in {} yet.".format(
            self.__class__.__name__
        )
        return self.get_agent(agent_id).get_state()

    def set_agent_state(
        self,
        position: List[float],
        rotation: List[float],
        agent_id: int = 0,
        reset_sensors: bool = True,
    ) -> bool:
        r"""Sets agent state similar to initialize_agent, but without agents
        creation. On failure to place the agent in the proper position, it is
        moved back to its previous pose.

        Args:
            position: list containing 3 entries for (x, y, z).
            rotation: list with 4 entries for (x, y, z, w) elements of unit
                quaternion (versor) representing agent 3D orientation,
                (https://en.wikipedia.org/wiki/Versor)
            agent_id: int identification of agent from multiagent setup.
            reset_sensors: bool for if sensor changes (e.g. tilt) should be
                reset).

        Returns:
            True if the set was successful else moves the agent back to its
            original pose and returns false.
        """
        agent = self.get_agent(agent_id)
        new_state = self.get_agent_state(agent_id)
        new_state.position = position
        new_state.rotation = rotation

        # NB: The agent state also contains the sensor states in _absolute_
        # coordinates. In order to set the agent's body to a specific
        # location and have the sensors follow, we must not provide any
        # state for the sensors. This will cause them to follow the agent's
        # body
        new_state.sensor_states = dict()
        agent.set_state(new_state, reset_sensors)
        return True

    def get_observations_at(
        self,
        position: Optional[List[float]] = None,
        rotation: Optional[List[float]] = None,
        keep_agent_at_new_pose: bool = False,
    ) -> Optional[Observations]:
        current_state = self.get_agent_state()
        if position is None or rotation is None:
            success = True
        else:
            success = self.set_agent_state(
                position, rotation, reset_sensors=False
            )

        if success:
            sim_obs = self.get_sensor_observations()

            self._prev_sim_obs = sim_obs

            observations = self._sensor_suite.get_observations(sim_obs)
            if not keep_agent_at_new_pose:
                self.set_agent_state(
                    current_state.position,
                    current_state.rotation,
                    reset_sensors=False,
                )
            return observations
        else:
            return None

    def try_step(self, position_a, position_b):
        return self.pathfinder.try_step(position_a, position_b)

    def distance_to_closest_obstacle(self, position, max_search_radius=2.0):
        return self.pathfinder.distance_to_closest_obstacle(
            position, max_search_radius
        )

    def island_radius(self, position):
        return self.pathfinder.island_radius(position)

    @property
    def previous_step_collided(self):
        r"""Whether or not the previous step resulted in a collision

        Returns:
            bool: True if the previous step resulted in a collision, false otherwise

        Warning:
            This feild is only updated when :meth:`step`, :meth:`reset`, or :meth:`get_observations_at` are
            called.  It does not update when the agent is moved to a new loction.  Furthermore, it
            will _always_ be false after :meth:`reset` or :meth:`get_observations_at` as neither of those
            result in an action (step) being taken.
        """
        return self._prev_sim_obs.get("collided", False)
