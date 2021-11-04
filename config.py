import os

import torch
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
# DEVICE = 'cpu'

REPRESENTATION_NAMES = ['keypoints3d','depth_euclidean']

FC_NEURON_LISTS = [8*len(REPRESENTATION_NAMES)*16*16,1024,1024,8*len(REPRESENTATION_NAMES)*16*16]
RESIDUAL_LAYERS_PER_BLOCK = [2,2,2,2]
RESIDUAL_SIZE   = [32, 64, 128, 256]
RESIDUAL_NEURON_CHANNEL   = [16, 8, 4, 2, 2]
STRIDES = [1, 1, 1]
IMG_DIMENSIONS = (3, 256, 256) # mid level reps are in colour right now
# MAP_DIMENSIONS =
BATCHSIZE = 4

MAP_SIZE = 5 # map size (in [m]), given a 256x256 map, picking map size = 5 gives a resolution of ~2cm


# TODO add habitat_config yaml path
# TODO add data path for use in habitat_config

HABITAT_LAB_REPO_PATH = '../habitat-lab'
HABITAT_CONFIGS_PATH = 'configs/'
