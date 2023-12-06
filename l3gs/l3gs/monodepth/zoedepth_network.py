from dataclasses import dataclass, field
from typing import Tuple, Type

import torch
from torchvision import transforms, datasets
from nerfstudio.configs.base_config import InstantiateConfig
from PIL import Image
import numpy as np
# from nerfstudio.viewer_beta.viewer_elements import ViewerText


@dataclass
class ZoeDepthNetworkConfig(InstantiateConfig):
    _target: Type = field(default_factory=lambda: ZoeDepthNetwork)
    depth_model: str = 'ZoeD_NK' #ZoeD_NK, ZoeD_N, ZoeD_K
    repo = "isl-org/ZoeDepth"
    device: str = 'cuda'


class ZoeDepthNetwork():
    def __init__(self, config: ZoeDepthNetworkConfig):
        # super().__init__()
        self.config = config

        self.model = torch.hub.load(self.config.repo, self.config.depth_model, pretrained=True)
        # self.model = self.model.to(self.config.device)


    @property
    def name(self) -> str:
        return "depth_{}".format(self.config.depth_model)

    def get_depth(self, image):
        if type(image) == Image or type(image) == np.ndarray:
            image = transforms.ToTensor()(image).to(self.config.device).unsqueeze(0)
        elif type(image) == torch.Tensor:
            image = (image).to(self.config.device).unsqueeze(0)
        else:
            raise Exception("Image type not supported")
        depth = self.model.to(self.config.device).infer(image)
        return depth


    
