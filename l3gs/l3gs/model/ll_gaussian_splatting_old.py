# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
NeRF implementation that combines many recent advancements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Type, Union
import torch
from nerfstudio.data.scene_box import OrientedBox
from torch.nn import Parameter
from torchmetrics.image import PeakSignalNoiseRatio, StructuralSimilarityIndexMeasure
from torchmetrics.image.lpip import LearnedPerceptualImagePatchSimilarity
import torchvision.transforms.functional as TF
from typing_extensions import Literal

from nerfstudio.cameras.cameras import Cameras
from gsplat.cuda_legacy._torch_impl import quat_to_rotmat

try:
    from gsplat.rendering import rasterization
except ImportError:
    print("Please install gsplat>=1.0.0")
from gsplat.cuda_legacy._wrapper import num_sh_bases

from nerfstudio.engine.callbacks import TrainingCallback, TrainingCallbackAttributes, TrainingCallbackLocation
from nerfstudio.engine.optimizers import Optimizers
from nerfstudio.models.base_model import Model, ModelConfig
import math
import numpy as np
from sklearn.neighbors import NearestNeighbors
import viser.transforms as vtf
# from nerfstudio.model_components.losses import scale_gauss_gradients_by_distance_squared
from nerfstudio.viewer.viewer_elements import ViewerButton, ViewerSlider, ViewerControl, ViewerVec3
# from nerfstudio.cameras.camera_optimizers import CameraOptimizer, CameraOptimizerConfig
from nerfstudio.models.splatfacto import SplatfactoModelConfig, SplatfactoModel
from l3gs.fields.gaussian_lerf_field import GaussianLERFField
from l3gs.encoders.image_encoder import BaseImageEncoderConfig, BaseImageEncoder
from l3gs.field_components.gaussian_lerf_fieldheadnames import GaussianLERFFieldHeadNames
from nerfstudio.viewer.viewer import VISER_NERFSTUDIO_SCALE_RATIO

from nerfstudio.model_components.losses import depth_ranking_loss
from pytorch_msssim import  SSIM
from nerfstudio.utils.colormaps import apply_colormap

# need following import for background color override
from nerfstudio.model_components import renderers
from nerfstudio.models.base_model import Model, ModelConfig
from nerfstudio.utils.colors import get_color
from nerfstudio.utils.rich_utils import CONSOLE

import torch
from jaxtyping import Float
from torch import Tensor

def random_quat_tensor(N):
    """
    Defines a random quaternion tensor of shape (N, 4)
    """
    u = torch.rand(N)
    v = torch.rand(N)
    w = torch.rand(N)
    return torch.stack(
        [
            torch.sqrt(1 - u) * torch.sin(2 * math.pi * v),
            torch.sqrt(1 - u) * torch.cos(2 * math.pi * v),
            torch.sqrt(u) * torch.sin(2 * math.pi * w),
            torch.sqrt(u) * torch.cos(2 * math.pi * w),
        ],
        dim=-1,
    )

def RGB2SH(rgb):
    """
    Converts from RGB values [0,1] to the 0th spherical harmonic coefficient
    """
    C0 = 0.28209479177387814
    return (rgb - 0.5) / C0


def SH2RGB(sh):
    """
    Converts from the 0th spherical harmonic coefficient to RGB values [0,1]
    """
    C0 = 0.28209479177387814
    return sh * C0 + 0.5

def projection_matrix(znear, zfar, fovx, fovy, device: Union[str,torch.device]="cpu"):
    """
    Constructs an OpenGL-style perspective projection matrix.
    """
    t = znear * math.tan(0.5 * fovy)
    b = -t
    r = znear * math.tan(0.5 * fovx)
    l = -r
    n = znear
    f = zfar
    return torch.tensor(
        [
            [2 * n / (r - l), 0.0, (r + l) / (r - l), 0.0],
            [0.0, 2 * n / (t - b), (t + b) / (t - b), 0.0],
            [0.0, 0.0, (f + n) / (f - n), -1.0 * f * n / (f - n)],
            [0.0, 0.0, 1.0, 0.0],
        ],
        device=device,
    )

def resize_image(image: torch.Tensor, d: int):
    """
    Downscale images using the same 'area' method in opencv

    :param image shape [H, W, C]
    :param d downscale factor (must be 2, 4, 8, etc.)

    return downscaled image in shape [H//d, W//d, C]
    """
    import torch.nn.functional as tf

    image = image.to(torch.float32)
    weight = (1.0 / (d * d)) * torch.ones((1, 1, d, d), dtype=torch.float32, device=image.device)
    return tf.conv2d(image.permute(2, 0, 1)[:, None, ...], weight, stride=d).squeeze(1).permute(1, 2, 0)


@torch.compile()
def get_viewmat(optimized_camera_to_world):
    """
    function that converts c2w to gsplat world2camera matrix, using compile for some speed
    """
    R = optimized_camera_to_world[:, :3, :3]  # 3 x 3
    T = optimized_camera_to_world[:, :3, 3:4]  # 3 x 1
    # flip the z and y axes to align with gsplat conventions
    R = R * torch.tensor([[[1, -1, -1]]], device=R.device, dtype=R.dtype)
    # analytic matrix inverse to get world2camera matrix
    R_inv = R.transpose(1, 2)
    T_inv = -torch.bmm(R_inv, T)
    viewmat = torch.zeros(R.shape[0], 4, 4, device=R.device, dtype=R.dtype)
    viewmat[:, 3, 3] = 1.0  # homogenous
    viewmat[:, :3, :3] = R_inv
    viewmat[:, :3, 3:4] = T_inv
    return viewmat



@dataclass
class LLGaussianSplattingModelConfig(SplatfactoModelConfig):
    """Gaussian Splatting Model Config"""

    _target: Type = field(default_factory=lambda: LLGaussianSplattingModel)
    warmup_length: int = 1000
    """period of steps where refinement is turned off"""
    refine_every: int = 75
    """period of steps where gaussians are culled and densified"""
    resolution_schedule: int = 250
    """training starts at 1/d resolution, every n steps this is doubled"""
    background_color: Literal["random", "black", "white"] = "random"
    """Whether to randomize the background color."""
    num_downscales: int = 0
    """at the beginning, resolution is 1/2^d, where d is this number"""
    cull_alpha_thresh: float = 0.085
    """threshold of opacity for culling gaussians"""
    cull_scale_thresh: float = 0.5
    """threshold of scale for culling gaussians"""
    reset_alpha_every: int = 30
    """Every this many refinement steps, reset the alpha"""
    densify_grad_thresh: float = 0.0002
    """threshold of positional gradient norm for densifying gaussians"""
    densify_size_thresh: float = 0.01
    """below this size, gaussians are *duplicated*, otherwise split"""
    n_split_samples: int = 2
    """number of samples to split gaussians into"""
    sh_degree_interval: int = 1000
    """every n intervals turn on another sh degree"""
    cull_screen_size: float = 0.15
    """if a gaussian is more than this percent of screen space, cull it"""
    split_screen_size: float = 0.05
    """if a gaussian is more than this percent of screen space, split it"""
    stop_screen_size_at: int = 50000
    """stop culling/splitting at this step WRT screen size of gaussians"""
    random_init: bool = False
    """whether to initialize the positions uniformly randomly (not SFM points)"""
    num_random: int = 15
    """Number of gaussians to initialize if random init is used"""
    random_scale: float = 500.0
    """Size of the cube to initialize random gaussians within"""
    init_opacity: float = 0.2
    """Initial opacity of deprojected gaussians"""
    ssim_lambda: float = 0.2
    """weight of ssim loss"""
    stop_split_at: int = 50000
    """stop splitting at this step"""
    sh_degree: int = 3
    """maximum degree of spherical harmonics to use"""
    clip_loss_weight: float = 0.1
    """weight of clip loss"""
    use_scale_regularization: bool = False
    """If enabled, a scale regularization introduced in PhysGauss (https://xpandora.github.io/PhysGaussian/) is used for reducing huge spikey gaussians."""
    max_gauss_ratio: float = 10.0
    """threshold of ratio of gaussian max to min scale before applying regularization
    loss from the PhysGaussian paper
    """
    rasterize_mode: Literal["classic", "antialiased"] = "classic"
    """
    Classic mode of rendering will use the EWA volume splatting with a [0.3, 0.3] screen space blurring kernel. This
    approach is however not suitable to render tiny gaussians at higher or lower resolution than the captured, which
    results "aliasing-like" artifacts. The antialiased mode overcomes this limitation by calculating compensation factors
    and apply them to the opacities of gaussians to preserve the total integrated density of splats.

    However, PLY exported with antialiased rasterize mode is not compatible with classic mode. Thus many web viewers that
    were implemented for classic mode can not render antialiased mode PLY properly without modifications.
    """

class LLGaussianSplattingModel(SplatfactoModel):
    """Gaussian Splatting model

    Args:
        config: Gaussian Splatting configuration to instantiate model
    """

    config: LLGaussianSplattingModelConfig

    def __init__(self, *args, **kwargs):
        if "seed_points" in kwargs:
            self.seed_pts = kwargs["seed_points"]
        else:
            self.seed_pts = None
        super().__init__(*args, **kwargs)
        self.deprojected_new = []
        self.colors_new = []
        self.postBA = False
        self.localized_query = None

    def populate_modules(self):
        if self.seed_pts is not None and not self.config.random_init:
            self.means = torch.nn.Parameter(self.seed_pts[0])  # (Location, Color)
        else:
            self.means = torch.nn.Parameter((torch.rand((self.config.num_random, 3)) - 0.5) * self.config.random_scale)
        self.xys_grad_norm = None
        self.max_2Dsize = None
        distances, _ = self.k_nearest_sklearn(self.means.data, 3)
        distances = torch.from_numpy(distances)
        # find the average of the three nearest neighbors for each point and use that as the scale
        avg_dist = distances.mean(dim=-1, keepdim=True)/5.0
        self.scales = torch.nn.Parameter(torch.log(avg_dist.repeat(1, 3)))
        self.quats = torch.nn.Parameter(random_quat_tensor(self.num_points))
        dim_sh = num_sh_bases(self.config.sh_degree)

        self.gaussian_lerf_field = GaussianLERFField()
        self.datamanager = self.kwargs["datamanager"]
        self.image_encoder: BaseImageEncoder = self.kwargs["image_encoder"]

        if (
            self.seed_pts is not None
            and not self.config.random_init
            # We can have colors without points.
            and self.seed_points[1].shape[0] > 0
        ):
            shs = torch.zeros((self.seed_points[1].shape[0], dim_sh, 3)).float().cuda()
            if self.config.sh_degree > 0:
                shs[:, 0, :3] = RGB2SH(self.seed_points[1] / 255)
                shs[:, 1:, 3:] = 0.0
            else:
                CONSOLE.log("use color only optimization with sigmoid activation")
                shs[:, 0, :3] = torch.logit(self.seed_points[1] / 255, eps=1e-10)
            self.features_dc = torch.nn.Parameter(shs[:, 0, :])
            self.features_rest = torch.nn.Parameter(shs[:, 1:, :])
        else:
            self.features_dc = torch.nn.Parameter(torch.rand(self.num_points, 3))
            self.features_rest = torch.nn.Parameter(torch.zeros((self.num_points, dim_sh - 1, 3)))

        self.opacities = torch.nn.Parameter(torch.logit(0.01 * torch.ones(self.num_points, 1)))

        # metrics
        self.psnr = PeakSignalNoiseRatio(data_range=1.0)
        self.ssim = SSIM(data_range=1.0, size_average=True, channel=3)
        self.lpips = LearnedPerceptualImagePatchSimilarity(normalize=True)
        self.step = 0
        self.steps_since_add = 0

        self.crop_box: Optional[OrientedBox] = None
        # self.back_color = torch.zeros(3)
        if self.config.background_color == "random":
                self.background_color = torch.tensor(
                [0.1490, 0.1647, 0.2157]
                )  # This color is the same as the default background color in Viser. This would only affect the background color when rendering.        
        else:
            self.background_color = get_color(self.config.background_color)
        self.image_embeds = torch.nn.Embedding(1000, 16)
        self.appearance_nn = torch.nn.Sequential(
            torch.nn.Linear(16, 32),
            torch.nn.ReLU(),
            torch.nn.Linear(32, 32),
            torch.nn.ReLU(),
            torch.nn.Linear(32, 6),
            torch.nn.Sigmoid(),
        )
        self.viewer_control = ViewerControl()
        self.viser_scale_ratio = 0.1

        # self.crop_to_word = ViewerButton("Crop gaussians to word", cb_hook=self.crop_to_word_cb)
        self.frame_on_word = ViewerButton("Localize Query", cb_hook=self.localize_query_cb)
        # self.reset_crop = ViewerButton("Reset crop", cb_hook=self.reset_crop_cb)
        # self.crop_scale = ViewerSlider("Crop scale", 0.1, 0, 3.0, 0.01)
        self.relevancy_thresh = ViewerSlider("Relevancy Thresh", 0.0, 0, 1.0, 0.01)

        self._crop_center_init = None
        self.crop_ids = None #torch.ones_like(self.means[:,0],dtype=torch.bool)
        self.dropout_mask = None
        self.original_means = None
        self.clrs = None

        # self.camera_optimizer: CameraOptimizer = self.config.camera_optimizer.setup(
        #     num_cameras=self.num_train_data, device="cpu"
        # )

    @property
    def colors(self):
        if self.config.sh_degree > 0:
            return SH2RGB(self.features_dc)
        else:
            return torch.sigmoid(self.features_dc)

    @property
    def shs_0(self):
        return self.features_dc

    @property
    def shs_rest(self):
        return self.features_rest
    
    def load_state_dict(self, dict, **kwargs):  # type: ignore
        # resize the parameters to match the new number of points
        self.step = 30000
        newp = dict["means"].shape[0]
        self.means = torch.nn.Parameter(torch.zeros(newp, 3, device=self.device))
        self.scales = torch.nn.Parameter(torch.zeros(newp, 3, device=self.device))
        self.quats = torch.nn.Parameter(torch.zeros(newp, 4, device=self.device))
        self.opacities = torch.nn.Parameter(torch.zeros(newp, 1, device=self.device))
        self.features_dc = torch.nn.Parameter(torch.zeros(newp, 3, device=self.device))
        self.features_rest = torch.nn.Parameter(
            torch.zeros(newp, num_sh_bases(self.config.sh_degree) - 1, 3, device=self.device)
        )
        super().load_state_dict(dict, **kwargs)

    def k_nearest_sklearn(self, x: torch.Tensor, k: int):
        """
            Find k-nearest neighbors using sklearn's NearestNeighbors.
        x: The data tensor of shape [num_samples, num_features]
        k: The number of neighbors to retrieve
        """
        # Convert tensor to numpy array
        x_np = x.cpu().numpy()

        # Build the nearest neighbors model
        from sklearn.neighbors import NearestNeighbors

        nn_model = NearestNeighbors(n_neighbors=k + 1, algorithm="auto", metric="euclidean").fit(x_np)

        # Find the k-nearest neighbors
        distances, indices = nn_model.kneighbors(x_np)

        # Exclude the point itself from the result and return
        return distances[:, 1:].astype(np.float32), indices[:, 1:].astype(np.float32)
    
    def add_new_params_to_optimizer(self, optimizer, new_param_groups):
        """
        Adds new parameters to the optimizer, initializing necessary states.

        Args:
            optimizer (torch.optim.Optimizer): The existing optimizer.
            new_param_groups (dict): A dictionary of new parameters to add, categorized by group.
        """
        num_new = new_param_groups[0].shape[0]
        
        param = optimizer.param_groups[0]["params"][0]

        param_state = optimizer.state[param]
        

        repeat_dims = (num_new,) + tuple(1 for _ in range(param_state["exp_avg"].dim() - 1))

        
        param_state["exp_avg"] = torch.cat(
            [param_state["exp_avg"], torch.ones_like(param_state["exp_avg"][-1]).repeat(*repeat_dims) * 0.4],
            dim=0,
        )
        param_state["exp_avg_sq"] = torch.cat(
            [
                param_state["exp_avg_sq"],
                torch.ones_like(param_state["exp_avg_sq"][-1]).repeat(*repeat_dims) * 0.4,
            ],
            dim=0,
        )

        del optimizer.state[param]
        optimizer.state[new_param_groups[0]] = param_state

        optimizer.param_groups[0]["params"] = new_param_groups
        del param

    def add_deprojected_means(self, deprojected, colors, optimizers: Optimizers, step):
        if len(deprojected) > 0:
            with torch.no_grad():
                # import pdb; pdb.set_trace()
                # deprojected = torch.stack(deprojected, dim=0).to(self.device)
                # colors = torch.stack(colors, dim=0).to(self.device)
                deprojected = deprojected[0]
                colors = colors[0]
                numpts = len(deprojected)
                avg_dist = torch.ones_like(deprojected.mean(dim=-1).unsqueeze(-1)) * 0.02 #* 0.01

                # if self.clrs == None:
                #     self.clrs = torch.nn.Parameter(torch.cat([self.means.detach(), colors], dim=0))
                # else:
                #     self.clrs = torch.nn.Parameter(torch.cat([self.clrs.detach(), colors], dim=0))

                dim_sh = num_sh_bases(self.config.sh_degree)
                if colors.max() > 1.0:
                    colors = colors / 255
                    assert colors.max() <= 1.0
                
                shs = torch.zeros((colors.shape[0], dim_sh, 3)).float().cuda()
                if self.config.sh_degree > 0:
                    shs[:, 0, :3] = RGB2SH(colors)
                    shs[:, 1:, 3:] = 0.0
                else:
                    CONSOLE.log("use color only optimization with sigmoid activation")
                    shs[:, 0, :3] = torch.logit(colors, eps=1e-10)

                self.means = torch.nn.Parameter(torch.cat([self.means.detach(), deprojected], dim=0))
                self.scales = torch.nn.Parameter(torch.cat([self.scales.detach(), torch.log(avg_dist.repeat(1, 3)).float().cuda()], dim=0))
                self.quats = torch.nn.Parameter(torch.cat([self.quats.detach(), random_quat_tensor(numpts).float().cuda()]))
                self.features_dc = torch.nn.Parameter(torch.cat([self.features_dc.detach(), shs[:, 0, :].to(self.device)]))
                self.features_rest = torch.nn.Parameter(torch.cat([self.features_rest.detach(), shs[:, 1:, :].to(self.device)]))
                self.opacities = torch.nn.Parameter(torch.cat([self.opacities.detach(), torch.logit(self.config.init_opacity * torch.ones(numpts, 1)).to(self.device)], dim=0))
                

                self.xys_grad_norm = None
                self.vis_counts = None
                self.max_2Dsize = None
                
                num_new_points = deprojected.shape[0]
                
                # Adding only the new parameters to the optimizer
                # new_gaussian_params = [new_means, new_scales, new_quats, new_colors_all, new_opacities]
                param_groups = self.get_gaussian_param_groups()
                for group, param in param_groups.items():
                    if group == 'lerf':
                        continue
                    new_param = [param[0][-num_new_points:]]
                    self.add_new_params_to_optimizer(optimizers.optimizers[group], new_param)

                # if self.num_points == self.config.num_random + numpts:
                #     print("removing random init")
                #     with torch.no_grad():
                #         mask = torch.cat(
                #             (
                #             torch.ones(self.config.num_random, device=self.device, dtype=torch.bool),
                #             torch.zeros(self.num_points - self.config.num_random, device=self.device, dtype=torch.bool)
                #             )
                #         )
                #         deleted_mask = self.cull_gaussians(mask, max(0.02, self.config.init_opacity - 0.05))
                #         # import pdb; pdb.set_trace()
                #         self.remove_from_all_optim(optimizers, deleted_mask)
            
            ## Deproject Debug
            # means_freeze = self.means.data.clone().cpu()
            # colors_freeze = self.clrs.data.clone().cpu()
            # self.viewer_control.viser_server.add_point_cloud("deprojected", means_freeze.numpy(force=True) * VISER_NERFSTUDIO_SCALE_RATIO, colors_freeze.numpy(force=True), 0.1)
            # import pdb; pdb.set_trace()

            colors = colors.detach()
            deprojected = deprojected.detach()
            del colors
            del deprojected
            torch.cuda.empty_cache()
            self.deprojected_new.clear()
            self.colors_new.clear()
            self.steps_since_add = 0
            self.postBA = True

    def remove_from_optim(self, optimizer, deleted_mask, new_params):
        """removes the deleted_mask from the optimizer provided"""
        assert len(new_params) == 1
        # assert isinstance(optimizer, torch.optim.Adam), "Only works with Adam"

        param = optimizer.param_groups[0]["params"][0]
        param_state = optimizer.state[param]
        del optimizer.state[param]

        # Modify the state directly without deleting and reassigning.
        if "exp_avg" in param_state:
            param_state["exp_avg"] = param_state["exp_avg"][~deleted_mask]
            param_state["exp_avg_sq"] = param_state["exp_avg_sq"][~deleted_mask]

        # Update the parameter in the optimizer's param group.
        del optimizer.param_groups[0]["params"][0]
        del optimizer.param_groups[0]["params"]
        optimizer.param_groups[0]["params"] = new_params
        optimizer.state[new_params[0]] = param_state

    def remove_from_all_optim(self, optimizers, deleted_mask):
        param_groups = self.get_gaussian_param_groups()
        for group, param in param_groups.items():
            if group == 'lerf':
                continue
            self.remove_from_optim(optimizers.optimizers[group], deleted_mask, param)
        torch.cuda.empty_cache()
        
    def dup_in_optim(self, optimizer, dup_mask, new_params, n=2):
        """adds the parameters to the optimizer"""
        param = optimizer.param_groups[0]["params"][0]
        param_state = optimizer.state[param]
        if "exp_avg" in param_state:
            repeat_dims = (n,) + tuple(1 for _ in range(param_state["exp_avg"].dim() - 1))
            param_state["exp_avg"] = torch.cat(
                [
                    param_state["exp_avg"],
                    torch.zeros_like(param_state["exp_avg"][dup_mask.squeeze()]).repeat(*repeat_dims),
                ],
                dim=0,
            )
            param_state["exp_avg_sq"] = torch.cat(
                [
                    param_state["exp_avg_sq"],
                    torch.zeros_like(param_state["exp_avg_sq"][dup_mask.squeeze()]).repeat(*repeat_dims),
                ],
                dim=0,
            )
        del optimizer.state[param]
        optimizer.state[new_params[0]] = param_state
        optimizer.param_groups[0]["params"] = new_params
        del param

    def dup_in_all_optim(self, optimizers, dup_mask, n):
        param_groups = self.get_gaussian_param_groups()
        for group, param in param_groups.items():
            if group == 'lerf':
                continue
            self.dup_in_optim(optimizers.optimizers[group], dup_mask, param, n)

    def after_train(self, step: int):
        assert step == self.step
        # to save some training time, we no longer need to update those stats post refinement
        if self.step >= self.config.stop_split_at:
            return
        with torch.no_grad():
            # keep track of a moving average of grad norms
            visible_mask = (self.radii > 0).flatten()
            assert self.xys.grad is not None
            grads = self.xys.grad.detach().norm(dim=-1)
            # print(f"grad norm min {grads.min().item()} max {grads.max().item()} mean {grads.mean().item()} size {grads.shape}")
            if self.xys_grad_norm is None:
                self.xys_grad_norm = grads
                self.vis_counts = torch.ones_like(self.xys_grad_norm)
            else:
                assert self.vis_counts is not None
                self.vis_counts[visible_mask] = self.vis_counts[visible_mask] + 1
                self.xys_grad_norm[visible_mask] = grads[visible_mask] + self.xys_grad_norm[visible_mask]

            # update the max screen size, as a ratio of number of pixels
            if self.max_2Dsize is None:
                self.max_2Dsize = torch.zeros_like(self.radii, dtype=torch.float32)
            newradii = self.radii.detach()[visible_mask]
            self.max_2Dsize[visible_mask] = torch.maximum(
                self.max_2Dsize[visible_mask],
                newradii / float(max(self.last_size[0], self.last_size[1])),
            )

    def set_crop(self, crop_box: Optional[OrientedBox]):
        self.crop_box = crop_box

    def set_background(self, background_color: torch.Tensor):
        assert background_color.shape == (3,)
        self.background_color = background_color

    def refinement_after(self, optimizers: Optimizers, step):
        assert step == self.step
        if self.step <= self.config.warmup_length:
            return
        deleted_mask = None
        with torch.no_grad():
            # Offset all the opacity reset logic by refine_every so that we don't
            # save checkpoints right when the opacity is reset (saves every 2k)
            # then cull
            # only split/cull if we've seen every image since opacity reset
            reset_interval = self.config.reset_alpha_every * self.config.refine_every
            do_densification = (
                self.step < self.config.stop_split_at
                and self.step % reset_interval > self.num_train_data + self.config.refine_every
            )
            if do_densification:
                # then we densify
                assert self.xys_grad_norm is not None and self.vis_counts is not None and self.max_2Dsize is not None
                avg_grad_norm = (self.xys_grad_norm / self.vis_counts) * 0.5 * max(self.last_size[0], self.last_size[1])
                high_grads = (avg_grad_norm > self.config.densify_grad_thresh).squeeze()
                splits = (self.scales.exp().max(dim=-1).values > self.config.densify_size_thresh).squeeze()
                if self.step < self.config.stop_screen_size_at:
                    splits |= (self.max_2Dsize > self.config.split_screen_size).squeeze()
                splits &= high_grads
                nsamps = self.config.n_split_samples
                (
                    split_means,
                    split_features_dc,
                    split_features_rest,
                    split_opacities,
                    split_scales,
                    split_quats,
                ) = self.split_gaussians(splits, nsamps)

                dups = (self.scales.exp().max(dim=-1).values <= self.config.densify_size_thresh).squeeze()
                dups &= high_grads
                (
                    dup_means,
                    dup_features_dc,
                    dup_features_rest,
                    dup_opacities,
                    dup_scales,
                    dup_quats,
                ) = self.dup_gaussians(dups)
                self.means = Parameter(torch.cat([self.means.detach(), split_means, dup_means], dim=0))
                self.features_dc = Parameter(
                    torch.cat(
                        [self.features_dc.detach(), split_features_dc, dup_features_dc],
                        dim=0,
                    )
                )
                self.features_rest = Parameter(
                    torch.cat(
                        [
                            self.features_rest.detach(),
                            split_features_rest,
                            dup_features_rest,
                        ],
                        dim=0,
                    )
                )
                self.opacities = Parameter(torch.cat([self.opacities.detach(), split_opacities, dup_opacities], dim=0))
                self.scales = Parameter(torch.cat([self.scales.detach(), split_scales, dup_scales], dim=0))
                self.quats = Parameter(torch.cat([self.quats.detach(), split_quats, dup_quats], dim=0))
                # append zeros to the max_2Dsize tensor
                self.max_2Dsize = torch.cat(
                    [
                        self.max_2Dsize,
                        torch.zeros_like(split_scales[:, 0]),
                        torch.zeros_like(dup_scales[:, 0]),
                    ],
                    dim=0,
                )

                split_idcs = torch.where(splits)[0]
                self.dup_in_all_optim(optimizers, split_idcs, nsamps)

                dup_idcs = torch.where(dups)[0]
                self.dup_in_all_optim(optimizers, dup_idcs, 1)

                # After a guassian is split into two new gaussians, the original one should also be pruned.
                splits_mask = torch.cat(
                    (
                        splits,
                        torch.zeros(
                            nsamps * splits.sum() + dups.sum(),
                            device=self.device,
                            dtype=torch.bool,
                        ),
                    )
                )
                
                if self.steps_since_add >= 5500 and self.postBA and self.steps_since_add < 10000:
                    deleted_mask = self.cull_gaussians(splits_mask)
            elif self.step >= self.config.stop_split_at and self.config.continue_cull_post_densification:
                if self.steps_since_add >= 5500 and self.postBA and self.steps_since_add < 10000:
                    deleted_mask = self.cull_gaussians()
            else:
                # if we donot allow culling post refinement, no more gaussians will be pruned.
                deleted_mask = None

            if deleted_mask is not None:
                self.remove_from_all_optim(optimizers, deleted_mask)
            # import pdb; pdb.set_trace()

            if self.step < self.config.stop_split_at and self.step % reset_interval == self.config.refine_every:
                # Reset value is set to be twice of the cull_alpha_thresh
                reset_value = self.config.cull_alpha_thresh * 2.0
                self.opacities.data = torch.clamp(
                    self.opacities.data,
                    max=torch.logit(torch.tensor(reset_value, device=self.device)).item(),
                )
                # reset the exp of optimizer
                optim = optimizers.optimizers["opacity"]
                param = optim.param_groups[0]["params"][0]
                param_state = optim.state[param]
                param_state["exp_avg"] = torch.zeros_like(param_state["exp_avg"])
                param_state["exp_avg_sq"] = torch.zeros_like(param_state["exp_avg_sq"])

            self.xys_grad_norm = None
            self.vis_counts = None
            self.max_2Dsize = None
    
    def cull_gaussians(self, extra_cull_mask: Optional[torch.Tensor] = None, override_cull_alpha_thresh: Optional[float] = None):
        """
        This function deletes gaussians with under a certain opacity threshold
        extra_cull_mask: a mask indicates extra gaussians to cull besides existing culling criterion
        """
        n_bef = self.num_points
        # cull transparent ones
        if override_cull_alpha_thresh is not None:
            culls = (torch.sigmoid(self.opacities) < override_cull_alpha_thresh).squeeze()
        else:
            culls = (torch.sigmoid(self.opacities) < self.config.cull_alpha_thresh).squeeze()
        below_alpha_count = torch.sum(culls).item()
        toobigs_count = 0
        if extra_cull_mask is not None:
            culls = culls | extra_cull_mask
        if self.step > self.config.refine_every * self.config.reset_alpha_every:
            # cull huge ones
            toobigs = (torch.exp(self.scales).max(dim=-1).values > self.config.cull_scale_thresh).squeeze()
            if self.step < self.config.stop_screen_size_at:
                # cull big screen space
                assert self.max_2Dsize is not None
                toobigs = toobigs | (self.max_2Dsize > self.config.cull_screen_size).squeeze()
            culls = culls | toobigs
            toobigs_count = torch.sum(toobigs).item()
        self.means = Parameter(self.means[~culls].detach())
        self.scales = Parameter(self.scales[~culls].detach())
        self.quats = Parameter(self.quats[~culls].detach())
        self.features_dc = Parameter(self.features_dc[~culls].detach())
        self.features_rest = Parameter(self.features_rest[~culls].detach())
        self.opacities = Parameter(self.opacities[~culls].detach())

        CONSOLE.log(
            f"Culled {n_bef - self.num_points} gaussians "
            f"({below_alpha_count} below alpha thresh, {toobigs_count} too bigs, {self.num_points} remaining)"
        )

        return culls

    def split_gaussians(self, split_mask, samps):
        """
        This function splits gaussians that are too large
        """

        n_splits = split_mask.sum().item()
        CONSOLE.log(f"Splitting {split_mask.sum().item()/self.num_points} gaussians: {n_splits}/{self.num_points}")
        centered_samples = torch.randn((samps * n_splits, 3), device=self.device)  # Nx3 of axis-aligned scales
        scaled_samples = (
            torch.exp(self.scales[split_mask].repeat(samps, 1)) * centered_samples
        )  # how these scales are rotated
        quats = self.quats[split_mask] / self.quats[split_mask].norm(dim=-1, keepdim=True)  # normalize them first
        rots = quat_to_rotmat(quats.repeat(samps, 1))  # how these scales are rotated
        rotated_samples = torch.bmm(rots, scaled_samples[..., None]).squeeze()
        new_means = rotated_samples + self.means[split_mask].repeat(samps, 1)
        # step 2, sample new colors
        new_features_dc = self.features_dc[split_mask].repeat(samps, 1)
        new_features_rest = self.features_rest[split_mask].repeat(samps, 1, 1)
        # step 3, sample new opacities
        new_opacities = self.opacities[split_mask].repeat(samps, 1)
        # step 4, sample new scales
        size_fac = 1.6
        new_scales = torch.log(torch.exp(self.scales[split_mask]) / size_fac).repeat(samps, 1)
        self.scales[split_mask] = torch.log(torch.exp(self.scales[split_mask]) / size_fac)
        # step 5, sample new quats
        new_quats = self.quats[split_mask].repeat(samps, 1)
        return (
            new_means,
            new_features_dc,
            new_features_rest,
            new_opacities,
            new_scales,
            new_quats,
        )
    
    def dup_gaussians(self, dup_mask):
        """
        This function duplicates gaussians that are too small
        """
        n_dups = dup_mask.sum().item()
        CONSOLE.log(f"Duplicating {dup_mask.sum().item()/self.num_points} gaussians: {n_dups}/{self.num_points}")
        dup_means = self.means[dup_mask]
        dup_features_dc = self.features_dc[dup_mask]
        dup_features_rest = self.features_rest[dup_mask]
        dup_opacities = self.opacities[dup_mask]
        dup_scales = self.scales[dup_mask]
        dup_quats = self.quats[dup_mask]
        return (
            dup_means,
            dup_features_dc,
            dup_features_rest,
            dup_opacities,
            dup_scales,
            dup_quats,
        )

    @property
    def num_points(self):
        return self.means.shape[0]
    
    def step_cb(self, step):
        self.step = step
        self.steps_since_add += 1

    def get_training_callbacks(
        self, training_callback_attributes: TrainingCallbackAttributes
    ) -> List[TrainingCallback]:
        cbs = []
        cbs.append(TrainingCallback([TrainingCallbackLocation.BEFORE_TRAIN_ITERATION], self.step_cb))
        # The order of these matters
        cbs.append(
            TrainingCallback(
                [TrainingCallbackLocation.AFTER_TRAIN_ITERATION],
                self.after_train,
            )
        )
        cbs.append(
            TrainingCallback(
                [TrainingCallbackLocation.AFTER_TRAIN_ITERATION],
                self.refinement_after,
                update_every_num_iters=self.config.refine_every,
                args=[training_callback_attributes.optimizers],
            )
        )
        cbs.append(
            TrainingCallback(
                [TrainingCallbackLocation.AFTER_TRAIN_ITERATION],
                self.add_deprojected_means,
                args=[self.deprojected_new, self.colors_new, training_callback_attributes.optimizers],
            )
        )
        return cbs

    def get_gaussian_param_groups(self) -> Dict[str, List[Parameter]]:
        return {
            "xyz": [self.means],
            "features_dc": [self.features_dc],
            "features_rest": [self.features_rest],
            "opacity": [self.opacities],
            "scaling": [self.scales],
            "rotation": [self.quats],
            "lerf" : list(self.gaussian_lerf_field.parameters())
        }
    
    def _get_downscale_factor(self):
        # if self.training:
        #     return 2 ** max((self.config.num_downscales - self.step // self.config.resolution_schedule), 0)
        # else:
        #     return 1
        return 1

    def get_param_groups(self) -> Dict[str, List[Parameter]]:
        """Obtain the parameter groups for the optimizers

        Returns:
            Mapping of different parameter groups
        """
        gps = self.get_gaussian_param_groups()
        gps["appearance_embed"] = list(self.appearance_nn.parameters()) + list(self.image_embeds.parameters())
        return gps

    def project_gaussians(self, camera, downscale_factor=1):
        if not isinstance(camera, Cameras):
            print("Called get_outputs with not a camera")
            return {}
        assert camera.shape[0] == 1, "Only one camera at a time"
        # if self.training:
            #currently relies on the branch vickie/camera-grads
            # self.camera_optimizer.apply_to_camera(camera)
        if self.crop_box is not None and not self.training:
            crop_ids = self.crop_box.within(self.means).squeeze()
            if crop_ids.sum() == 0:
                return {"rgb": torch.full((camera.height.item(), camera.width.item(), 3), 0.5, device=self.device)}
        else:
            crop_ids = None
        camera_downscale = downscale_factor
        camera.rescale_output_resolution(1 / camera_downscale)
        # shift the camera to center of scene looking at center
        R = camera.camera_to_worlds[0, :3, :3]  # 3 x 3
        T = camera.camera_to_worlds[0, :3, 3:4]  # 3 x 1
        # flip the z axis to align with gsplat conventions
        R_edit = torch.tensor(vtf.SO3.from_x_radians(np.pi).as_matrix(), device=R.device, dtype=R.dtype)
        R = R @ R_edit
        # analytic matrix inverse to get world2camera matrix
        R_inv = R.T
        T_inv = -R_inv @ T
        viewmat = torch.eye(4, device=R.device, dtype=R.dtype)
        viewmat[:3, :3] = R_inv
        viewmat[:3, 3:4] = T_inv
        # calculate the FOV of the camera given fx and fy, width and height
        cx = camera.cx.item()
        cy = camera.cy.item()
        fovx = 2 * math.atan(camera.width / (2 * camera.fx))
        fovy = 2 * math.atan(camera.height / (2 * camera.fy))
        W, H = camera.width.item(), camera.height.item()
        self.last_size = (H, W)
        projmat = projection_matrix(0.001, 1000, fovx, fovy, device=self.device)
        # BLOCK_X, BLOCK_Y = 16, 16
        # tile_bounds = (
        #     (W + BLOCK_X - 1) // BLOCK_X,
        #     (H + BLOCK_Y - 1) // BLOCK_Y,
        #     1,
        # )

        if crop_ids is not None:
            means_crop = self.means[crop_ids]
            scales_crop = self.scales[crop_ids]
            quats_crop = self.quats[crop_ids]
        else:
            means_crop = self.means
            scales_crop = self.scales
            quats_crop = self.quats
        BLOCK_WIDTH = 16
        xys, depths, radii, conics, comp, num_tiles_hit, cov3d = project_gaussians(
            means_crop,
            torch.exp(scales_crop),
            1,
            quats_crop / quats_crop.norm(dim=-1, keepdim=True),
            viewmat.squeeze()[:3, :],
            projmat.squeeze() @ viewmat.squeeze(),
            camera.fx.item(),
            camera.fy.item(),
            cx,
            cy,
            H,
            W,
            BLOCK_WIDTH,
        )

        # rescale the camera back to original dimensions
        camera.rescale_output_resolution(camera_downscale)
        return xys, depths, radii, conics, num_tiles_hit, cov3d, W, H
    
    def get_outputs(self, camera: Cameras) -> Dict[str, Union[torch.Tensor, List]]:
        """Takes in a Ray Bundle and returns a dictionary of outputs.

        Args:
            ray_bundle: Input bundle of rays. This raybundle should have all the
            needed information to compute the outputs.

        Returns:
            Outputs of model. (ie. rendered colors)
        """
        if not isinstance(camera, Cameras):
            print("Called get_outputs with not a camera")
            return {}
        assert camera.shape[0] == 1, "Only one camera at a time"
        outputs = {}
        
        # get the background color
        if self.training:
            if self.config.background_color == "random":
                background = torch.rand(3, device=self.device)
            elif self.config.background_color == "white":
                background = torch.ones(3, device=self.device)
            elif self.config.background_color == "black":
                background = torch.zeros(3, device=self.device)
            else:
                background = self.background_color.to(self.device)
        else:
            if renderers.BACKGROUND_COLOR_OVERRIDE is not None:
                background = renderers.BACKGROUND_COLOR_OVERRIDE.to(self.device)
            else:
                background = self.background_color.to(self.device)

        if self.crop_box is not None and not self.training:
            crop_ids = self.crop_box.within(self.means).squeeze()
            if crop_ids.sum() == 0:
                return {"rgb": background.repeat(int(camera.height.item()), int(camera.width.item()), 1)}
        else:
            crop_ids = None

        camera_downscale = self._get_downscale_factor()

        R = camera.camera_to_worlds[0, :3, :3]  # 3 x 3
        T = camera.camera_to_worlds[0, :3, 3:4]  # 3 x 1
        # flip the z and y axes to align with gsplat conventions
        R_edit = torch.diag(torch.tensor([1, -1, -1], device=self.device, dtype=R.dtype))
        R = R @ R_edit
        # analytic matrix inverse to get world2camera matrix
        R_inv = R.T
        T_inv = -R_inv @ T
        viewmat = torch.eye(4, device=R.device, dtype=R.dtype)
        viewmat[:3, :3] = R_inv
        viewmat[:3, 3:4] = T_inv
        # calculate the FOV of the camera given fx and fy, width and height
        cx = camera.cx.item()
        cy = camera.cy.item()
        fovx = 2 * math.atan(camera.width / (2 * camera.fx))
        fovy = 2 * math.atan(camera.height / (2 * camera.fy))
        W, H = int(camera.width.item()), int(camera.height.item())
        self.last_size = (H, W)
        projmat = projection_matrix(0.001, 1000, fovx, fovy, device=self.device)
        # BLOCK_X, BLOCK_Y = 16, 16
        # tile_bounds = (
        #     int((W + BLOCK_X - 1) // BLOCK_X),
        #     int((H + BLOCK_Y - 1) // BLOCK_Y),
        #     1,
        # )

        if crop_ids is not None:
            opacities_crop = self.opacities[crop_ids]
            means_crop = self.means[crop_ids]
            features_dc_crop = self.features_dc[crop_ids]
            features_rest_crop = self.features_rest[crop_ids]
            scales_crop = self.scales[crop_ids]
            quats_crop = self.quats[crop_ids]
        else:
            opacities_crop = self.opacities
            means_crop = self.means
            features_dc_crop = self.features_dc
            features_rest_crop = self.features_rest
            scales_crop = self.scales
            quats_crop = self.quats

        colors_crop = torch.cat((features_dc_crop[:, None, :], features_rest_crop), dim=1)
        BLOCK_WIDTH = 16
        self.xys, depths, self.radii, conics, comp, num_tiles_hit, cov3d = project_gaussians(  # type: ignore
            means_crop,
            torch.exp(scales_crop),
            1,
            quats_crop / quats_crop.norm(dim=-1, keepdim=True),
            viewmat.squeeze()[:3, :],
            projmat.squeeze() @ viewmat.squeeze(),
            camera.fx.item(),
            camera.fy.item(),
            cx,
            cy,
            H,
            W,
            BLOCK_WIDTH,
        )  # type: ignore
        if (self.radii).sum() == 0:
            return {"rgb": background.repeat(int(camera.height.item()), int(camera.width.item()), 1)}
        # if self.num_points == 50:
        #     import pdb; pdb.set_trace()
        if self.training:
            self.xys.retain_grad()

        if self.config.sh_degree > 0:
            viewdirs = means_crop.detach() - camera.camera_to_worlds.detach()[..., :3, 3]  # (N, 3)
            viewdirs = viewdirs / viewdirs.norm(dim=-1, keepdim=True)
            n = min(self.step // self.config.sh_degree_interval, self.config.sh_degree)
            rgbs = spherical_harmonics(n, viewdirs, colors_crop)
            rgbs = torch.clamp(rgbs + 0.5, min=0.0)  # type: ignore
        else:
            rgbs = torch.sigmoid(colors_crop[:, 0, :])

        # rescale the camera back to original dimensions
        camera.rescale_output_resolution(camera_downscale)
        assert (num_tiles_hit > 0).any()  # type: ignore

        opacities = None
        if self.config.rasterize_mode == "antialiased":
            opacities = torch.sigmoid(opacities_crop) * comp[:, None]
        elif self.config.rasterize_mode == "classic":
            opacities = torch.sigmoid(opacities_crop)
        else:
            raise ValueError("Unknown rasterize_mode: %s", self.config.rasterize_mode)

        rgb, alpha = rasterize_gaussians(  # type: ignore            
            self.xys,
            depths,
            self.radii,
            conics,
            num_tiles_hit,  # type: ignore
            rgbs,
            opacities,
            H,
            W,
            BLOCK_WIDTH,
            background=background,
            return_alpha=True,
        )  # type: ignore
        alpha = alpha[..., None]
        if camera.metadata is not None and "cam_idx" in camera.metadata:
            cam_id = camera.metadata["cam_idx"]
            embeds = self.image_embeds(torch.tensor(cam_id, device=self.device))
            affine_shift = self.appearance_nn(embeds)
            rgb = rgb * (1 + affine_shift[:3]) + affine_shift[3:]
        rgb = torch.clamp(rgb, max=1.0)  # type: ignore
        
        outputs["rgb"] = rgb
        outputs["accumulation"] = alpha
        
        depth_im = None
        depth_im = rasterize_gaussians(  # type: ignore
                self.xys,
                depths,
                self.radii,
                conics,
                num_tiles_hit,  # type: ignore
                depths[:, None].repeat(1, 3),
                opacities,
                H,
                W,
                BLOCK_WIDTH,
                background=torch.ones(3, device=self.device),
            )[..., 0:1]  # type: ignore
        depth_im = torch.where(alpha > 0, depth_im / alpha, depth_im.detach().max())
        outputs["depth"] = depth_im

        # with torch.no_grad():
        #     depth_xys, depth_depths, depth_radii, depth_conics, depth_num_tiles_hit, depth_cov3d = project_gaussians(  # type: ignore
        #         means_crop,
        #         torch.exp(scales_crop),
        #         1,
        #         quats_crop / quats_crop.norm(dim=-1, keepdim=True),
        #         viewmat.squeeze()[:3, :],
        #         projmat.squeeze() @ viewmat.squeeze(),
        #         camera.fx.item(),
        #         camera.fy.item(),
        #         cx//2,
        #         cy//2,
        #         H//2,
        #         W//2,
        #         BLOCK_WIDTH,
        #     ) 
        #     depth_im = rasterize_gaussians(  # type: ignore
        #         depth_xys,
        #         depth_depths,
        #         depth_radii,
        #         depth_conics,
        #         depth_num_tiles_hit,  # type: ignore
        #         depths[:, None].repeat(1, 3),
        #         torch.sigmoid(opacities_crop),
        #         H//2,
        #         W//2,
        #         background=torch.ones(3, device=self.device),
        #     )[..., 0:1]  # type: ignore
        #     # alpha.resize_(H//2, W//2, 1)
        #     # depth_im = torch.where(alpha > 0, depth_im / alpha, depth_im.detach().max())
        #     outputs["depth_half"] = depth_im
        
        if self.datamanager.use_clip:
            # import pdb; pdb.set_trace()
            if self.step - self.datamanager.lerf_step > 500:
                if camera.metadata is not None:
                    if "clip_downscale_factor" not in camera.metadata:
                        return outputs
                ########################
                # CLIP Relevancy Field #
                ########################
                reset_interval = self.config.reset_alpha_every * self.config.refine_every
                if self.training and self.step>self.config.warmup_length and (self.step % reset_interval > self.num_train_data + self.config.refine_every  or self.step < (self.config.reset_alpha_every * self.config.refine_every)):

                    with torch.no_grad():
                        clip_xys, clip_depths, clip_radii, clip_conics, clip_num_tiles_hit, clip_cov3d, clip_W, clip_H = self.project_gaussians(camera, downscale_factor=camera.metadata["clip_downscale_factor"])

                        self.random_pixels = self.datamanager.random_pixels.to(self.device)

                    clip_scale = self.datamanager.curr_scale * torch.ones((self.random_pixels.shape[0],1),device=self.device)
                    clip_scale = clip_scale * clip_H * (depth_im.view(-1, 1)[self.random_pixels] / camera.fy.item())

                    clip_hash_encoding = self.gaussian_lerf_field.get_hash(self.means)

                    field_output = rasterize_gaussians(
                        clip_xys.detach(),
                        clip_depths.detach(),
                        clip_radii.detach(),
                        clip_conics.detach(),
                        clip_num_tiles_hit,
                        # clip_hash_encoding[self.dropout_mask] / clip_hash_encoding[self.dropout_mask].norm(dim=-1, keepdim=True),
                        # clip_hash_encoding[self.dropout_mask],
                        clip_hash_encoding,
                        torch.sigmoid(opacities_crop.detach().clone()),
                        clip_H,
                        clip_W,
                        BLOCK_WIDTH,
                        torch.zeros(clip_hash_encoding.shape[1], device=self.device),
                    )

                    field_output = self.gaussian_lerf_field.get_outputs_from_feature(field_output.view(clip_H*clip_W, -1)[self.random_pixels], clip_scale)

                    clip_output = field_output[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)

                    outputs["clip"] = clip_output
                    outputs["clip_scale"] = clip_scale

                if not self.training:
                    # N x B x 1; N
                    max_across, self.best_scales = self.get_max_across(
                        self.xys,
                        depths,
                        self.radii,
                        conics,
                        num_tiles_hit,
                        torch.sigmoid(self.opacities[crop_ids]),
                        H,
                        W,
                    )

                    for i in range(len(self.image_encoder.positives)):
                        max_across[i][max_across[i] < self.relevancy_thresh.value] = 0
                        # relevancy_rasterized[relevancy_rasterized < 0.5] = 0
                        outputs[f"relevancy_{i}"] = max_across[i].view(H, W, -1)
                        # outputs[f"relevancy_rasterized_{i}"] = relevancy_rasterized.view(H, W, -1)
                        # outputs[f"best_scales_{i}"] = best_scales[i]
                
        return outputs
    
    def depth_ranking_loss(
        self,
        rendered_depth: Float[Tensor, "*batch H W"],
        gt_depth: Float[Tensor, "*batch H W"],
        mask: Float[Tensor, "*batch H W"],
        patch_size: int = 128,
        num_patches: int = 8,
        epsilon: float = 1e-6,
        ) -> Float[Tensor, "*batch"]:
        """
        Depth ranking loss as described in the SparseGS paper.
        Args:
            rendered_depth: rendered depth image
            gt_depth: ground truth depth image
            mask: mask for the depth images. 1 where valid, 0 where invalid
            patch_size: patch size
            num_patches: number of patches to sample
            epsilon: small value to avoid division by zero
        """
        # import pdb; pdb.set_trace()
        b, h, w = rendered_depth.shape
        # construct patch indices
        sh = torch.randint(0, h - patch_size, (b, num_patches, patch_size, patch_size))
        sw = torch.randint(0, w - patch_size, (b, num_patches, patch_size, patch_size))
        idx_batch = torch.arange(b)[:, None, None, None].repeat(1, num_patches, patch_size, patch_size)
        idx_rows = torch.arange(patch_size)[None, None, None, :].repeat(b, 1, 1, 1) + sh
        idx_cols = torch.arange(patch_size)[None, None, None, :].repeat(b, 1, 1, 1) + sw
        # index into and mask out patches
        mask_patches = mask[idx_batch, idx_rows, idx_cols]
        rendered_depth_patches = rendered_depth[idx_batch, idx_rows, idx_cols] * mask_patches
        gt_depth_patches = gt_depth[idx_batch, idx_rows, idx_cols] * mask_patches
        # calculate correlation
        e_xy = torch.mean(rendered_depth_patches * gt_depth_patches, dim=[-1, -2])
        e_x = torch.mean(rendered_depth_patches, dim=[-1, -2])
        e_y = torch.mean(gt_depth_patches, dim=[-1, -2])
        e_x2 = torch.mean(torch.square(rendered_depth_patches), dim=[-1, -2])
        ex_2 = e_x**2
        e_y2 = torch.mean(torch.square(gt_depth_patches), dim=[-1, -2])
        ey_2 = e_y**2
        corr = (e_xy - e_x * e_y) / (torch.sqrt((e_y2 - ey_2) * (e_x2 - ex_2)) + epsilon)
        corr = torch.clamp(corr, min=-1, max=1)
        # calculate loss
        loss = torch.mean(1 - corr, dim=-1)
        return loss

    def get_gt_img(self, image: torch.Tensor):
        """Compute groundtruth image with iteration dependent downscale factor for evaluation purpose

        Args:
            image: tensor.Tensor in type uint8 or float32
        """
        if image.dtype == torch.uint8:
            image = image.float() / 255.0
        d = self._get_downscale_factor()
        if d > 1:
            newsize = [image.shape[0] // d, image.shape[1] // d]

            # torchvision can be slow to import, so we do it lazily.
            import torchvision.transforms.functional as TF

            gt_img = TF.resize(image.permute(2, 0, 1), newsize, antialias=None).permute(1, 2, 0)
        else:
            gt_img = image
        return gt_img.to(self.device)

    def get_metrics_dict(self, outputs, batch) -> Dict[str, torch.Tensor]:
        """Compute and returns metrics.

        Args:
            outputs: the output to compute loss dict to
            batch: ground truth batch corresponding to outputs
        """
        gt_rgb = self.get_gt_img(batch["image"])
        metrics_dict = {}
        predicted_rgb = outputs["rgb"]
        # metrics_dict["psnr"] = self.psnr(predicted_rgb, gt_rgb)

        metrics_dict["gaussian_count"] = self.num_points
        return metrics_dict

    # @profile
    def get_loss_dict(self, outputs, batch, metrics_dict=None) -> Dict[str, torch.Tensor]:
        """Computes and returns the losses dict.

        Args:
            outputs: the output to compute loss dict to
            batch: ground truth batch corresponding to outputs
            metrics_dict: dictionary of metrics, some of which we can use for loss
        """
        loss_dict = {}
        gt_img = self.get_gt_img(batch["image"])
        pred_img = outputs["rgb"]

        # Set masked part of both ground-truth and rendered image to black.
        # This is a little bit sketchy for the SSIM loss.
        if "mask" in batch:
            # batch["mask"] : [H, W, 1]
            assert batch["mask"].shape[:2] == gt_img.shape[:2] == pred_img.shape[:2]
            mask = batch["mask"].to(self.device)
            gt_img = gt_img * mask
            pred_img = pred_img * mask

        Ll1 = torch.abs(gt_img - pred_img).mean()
        simloss = 1 - self.ssim(gt_img.permute(2, 0, 1)[None, ...], pred_img.permute(2, 0, 1)[None, ...])
        loss_dict["main_loss"] = (1 - self.config.ssim_lambda) * Ll1 + self.config.ssim_lambda * simloss

        # if "depth_half" in outputs.keys() and "depth" in batch.keys() and self.steps_since_add > 1100:
            
        #     assert outputs["depth_half"].shape == batch["depth"].shape
        #     gt_depth = batch["depth"].permute(2, 0, 1)
        #     pred_depth = outputs["depth_half"].permute(2, 0, 1)
        #     mask = (batch["depth"] < batch["depth"].mean() * 1.5) & (batch["depth"] > 0)
        #     mask = mask.permute(2, 0, 1)
        #     # import pdb; pdb.set_trace()
        #     assert pred_depth.shape == gt_depth.shape == mask.shape
        #     # import matplotlib.pyplot as plt
        #     # plt.imshow(batch["depth"].squeeze(-1).cpu().numpy())
        #     # plt.savefig("depth_gt.png")
        #     # plt.imshow(outputs["depth_half"].squeeze(-1).detach().cpu().numpy())
        #     # plt.savefig("depth_pred.png")
        #     # plt.imshow(mask.squeeze(0).cpu().numpy())
        #     # plt.savefig("depth_mask.png")
        #     # plt.imshow(batch["depth"].squeeze(-1).cpu().numpy() * mask.squeeze(0).cpu().numpy())
        #     # plt.savefig("depth_gt_masked.png")
        #     # plt.imshow(outputs["depth_half"].squeeze(-1).cpu().numpy() * mask.squeeze(0).cpu().numpy())
        #     # plt.savefig("depth_pred_masked.png")
        #     # plt.imshow(outputs["depth"].squeeze(-1).detach().cpu().numpy())
        #     # plt.savefig("depth_pred_full.png")
        #     # import pdb; pdb.set_trace()
        #     depth_correlation_loss = self.depth_ranking_loss(pred_depth, gt_depth, mask)
        #     loss_dict["depth_loss"] = depth_correlation_loss
        # if "depth" in outputs.keys() and "depth" in batch.keys() and self.steps_since_add > 2000:
        #     assert outputs["depth"].shape == batch["depth"].shape
        #     gt_depth = batch["depth"].permute(2, 0, 1)
        #     pred_depth = outputs["depth"].permute(2, 0, 1)
        #     mask = (batch["depth"] < batch["depth"].mean() * 1.5) & (batch["depth"] > 0)
        #     mask = mask.permute(2, 0, 1)
        #     # import pdb; pdb.set_trace()
        #     assert pred_depth.shape == gt_depth.shape == mask.shape
        #     depth_correlation_loss = self.depth_ranking_loss(pred_depth, gt_depth, mask)
        #     loss_dict["depth_loss"] = depth_correlation_loss

        if self.config.use_scale_regularization and self.step % 10 == 0:
            scale_exp = torch.exp(self.scales)
            scale_reg = (
                torch.maximum(
                    scale_exp.amax(dim=-1) / scale_exp.amin(dim=-1),
                    torch.tensor(self.config.max_gauss_ratio),
                )
                - self.config.max_gauss_ratio
            )
            scale_reg = 0.1 * scale_reg.mean()
        else:
            scale_reg = torch.tensor(0.0).to(self.device)
        loss_dict["scale_reg"] = scale_reg

        if self.training and 'clip' in outputs and 'clip' in batch: 
            unreduced_clip = self.config.clip_loss_weight * torch.nn.functional.huber_loss(
                outputs["clip"], batch["clip"].to(self.device).to(torch.float32), delta=1.25, reduction="none"
            )
            loss_dict["clip_loss"] = unreduced_clip.sum(dim=-1).nanmean()
        
        psnr = self.psnr(gt_img, pred_img)
        # print(f"PSNR: {psnr.item()}")
        # print(f"PSNR: {psnr.item()}")
        
        return loss_dict

    @torch.no_grad()
    def get_outputs_for_camera(self, camera: Cameras, obb_box: Optional[OrientedBox] = None) -> Dict[str, torch.Tensor]:
        """Takes in a camera, generates the raybundle, and computes the output of the model.
        Overridden for a camera-based gaussian model.

        Args:
            camera: generates raybundle
        """
        assert camera is not None, "must provide camera to gaussian model"
        self.set_crop(obb_box)
        outs = self.get_outputs(camera.to(self.device))
        return outs  # type: ignore

    def get_image_metrics_and_images(
        self, outputs: Dict[str, torch.Tensor], batch: Dict[str, torch.Tensor]
    ) -> Tuple[Dict[str, float], Dict[str, torch.Tensor]]:
        """Writes the test image outputs.

        Args:
            image_idx: Index of the image.
            step: Current step.
            batch: Batch of data.
            outputs: Outputs of the model.

        Returns:
            A dictionary of metrics.
        """
        gt_rgb = self.get_gt_img(batch["image"])
        d = self._get_downscale_factor()
        if d > 1:
            # torchvision can be slow to import, so we do it lazily.
            import torchvision.transforms.functional as TF

            newsize = [batch["image"].shape[0] // d, batch["image"].shape[1] // d]
            predicted_rgb = TF.resize(outputs["rgb"].permute(2, 0, 1), newsize, antialias=None).permute(1, 2, 0)
        else:
            predicted_rgb = outputs["rgb"]

        combined_rgb = torch.cat([gt_rgb, predicted_rgb], dim=1)

        # Switch images from [H, W, C] to [1, C, H, W] for metrics computations
        gt_rgb = torch.moveaxis(gt_rgb, -1, 0)[None, ...]
        predicted_rgb = torch.moveaxis(predicted_rgb, -1, 0)[None, ...]

        psnr = self.psnr(gt_rgb, predicted_rgb)
        ssim = self.ssim(gt_rgb, predicted_rgb)
        lpips = self.lpips(gt_rgb, predicted_rgb)

        # all of these metrics will be logged as scalars
        metrics_dict = {"psnr": float(psnr.item()), "ssim": float(ssim)}  # type: ignore
        print(f"PSNR: {psnr.item()}, SSIM: {ssim}, LPIPS: {lpips}")
        metrics_dict["lpips"] = float(lpips)

        images_dict = {"img": combined_rgb}

        return metrics_dict, images_dict

    def k_nearest_sklearn(self, x: torch.Tensor, k: int, include_self: bool = False):
        """
        Find k-nearest neighbors using sklearn's NearestNeighbors.
        x: The data tensor of shape [num_samples, num_features]
        k: The number of neighbors to retrieve
        """
        # Convert tensor to numpy array
        x_np = x.cpu().numpy()

        # Build the nearest neighbors model
        nn_model = NearestNeighbors(n_neighbors=k + 1, algorithm="auto", metric="euclidean").fit(x_np)

        # Find the k-nearest neighbors
        distances, indices = nn_model.kneighbors(x_np)

        if include_self:
            return distances.astype(np.float32), indices
        else:
            return distances[:, 1:].astype(np.float32), indices[:, 1:].astype(np.float32)
    
    def localize_query_cb(self,element):
        with torch.no_grad():
            # clip_feats = self.gaussian_lerf_field.get_outputs_from_feature(self.clip_hash / self.clip_hash.norm(dim=-1,keepdim=True), self.crop_scale.value * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            # clip_feats = self.gaussian_lerf_field.get_outputs(self.means, self.crop_scale.value * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            # clip_feats = self.gaussian_lerf_field.get_outputs(self.means, self.best_scales[0].to(self.device) * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)

            # Do K nearest neighbors for each point and then avg the clip hash for each point based on the KNN
            # import pdb; pdb.set_trace()
            means_freeze = self.means.data.clone().detach()
            distances, indicies = self.k_nearest_sklearn(means_freeze, 3, True)
            distances = torch.from_numpy(distances).to(self.device)
            indicies = torch.from_numpy(indicies).view(-1)
            weights = torch.sigmoid(self.opacities[indicies].view(-1, 4))
            weights = torch.nn.Softmax(dim=-1)(weights)
            points = means_freeze[indicies]
            # clip_hash_encoding = self.gaussian_lerf_field.get_hash(self.means)
            clip_hash_encoding = self.gaussian_lerf_field.get_hash(points)
            clip_hash_encoding = clip_hash_encoding.view(-1, 4, clip_hash_encoding.shape[1])
            clip_hash_encoding = (clip_hash_encoding * weights.unsqueeze(-1))
            clip_hash_encoding = clip_hash_encoding.sum(dim=1)
            clip_feats = self.gaussian_lerf_field.get_outputs_from_feature(clip_hash_encoding, self.best_scales[0].to(self.device) * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            relevancy = self.image_encoder.get_relevancy(clip_feats / (clip_feats.norm(dim=-1, keepdim=True)+1e-6), 0).view(self.num_points, -1)
            # color = apply_colormap(relevancy[..., 0:1])
            # self.viewer_control.viser_server.add_point_cloud("relevancy", self.means.numpy(force=True) * 10, color.numpy(force=True), 0.01)

            # Add a slider to debug the relevancy values
            
            # self.crop_ids = (relevancy[..., 0] > self.relevancy_thresh.value)
            
            #Define all crop viewer elements
            # self.crop_points = relevancy[..., 0] > self.relevancy_thresh.value
            # self._crop_center_init = self.means[self.crop_points].mean(dim=0).cpu().numpy()
            self._crop_center_init = means_freeze[relevancy[..., 0].argmax(dim=0).cpu().numpy()].cpu().numpy()
            # self.original_means = self.means.data.clone()
            
            query = self._crop_center_init / self.viser_scale_ratio

            # self.viewer_control.viser_server.add_icosphere(
            # "/query",
            # radius = 4, 
            # color = (1.0, 0.0, 0.0),
            # position=(query[0], query[1], query[2]),
            # )
            self.viewer_control.viser_server.add_frame(
            "/query",
            axes_length = 4, 
            axes_radius = 0.025 * 3,
            wxyz=(1.0, 0.0, 0.0, 0.0),
            position=(query[0], query[1], query[2]),
            )


            H = self.datamanager.train_dataset._dataparser_outputs.dataparser_transform
            row = torch.tensor([[0,0,0,1]],dtype=torch.float32,device=H.device)

            inv_H = torch.cat([torch.cat([H[:3, :3].transpose(1, 0), -H[:3, 3:]], dim=1), row], dim=0)
            query_world = inv_H @ torch.tensor([query[0], query[1], query[2], 1],dtype=torch.float32,device=H.device)
            print(query_world / VISER_NERFSTUDIO_SCALE_RATIO)

            self.localized_query = query_world[:3].cpu().numpy() / VISER_NERFSTUDIO_SCALE_RATIO
            

            # self._crop_handle = self.viewer_control.viser_server.add_transform_controls("Crop Points", depth_test=False, line_width=4.0)
            # world_center = tuple(p / self.viser_scale_ratio for p in self._crop_center_init)
            # self._crop_handle.position = world_center

            # self._crop_center.value = tuple(p / self.viser_scale_ratio for p in self._crop_center_init)

            # self.viewer_control.viser_server.add_point_cloud("Centroid", self._crop_center_init / self.viser_scale_ratio, np.array([0,0,0]), 0.1)


    def crop_to_word_cb(self,element):
        with torch.no_grad():
            # clip_feats = self.gaussian_lerf_field.get_outputs_from_feature(self.clip_hash / self.clip_hash.norm(dim=-1,keepdim=True), self.crop_scale.value * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            # clip_feats = self.gaussian_lerf_field.get_outputs(self.means, self.crop_scale.value * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            # clip_feats = self.gaussian_lerf_field.get_outputs(self.means, self.best_scales[0].to(self.device) * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)

            # Do K nearest neighbors for each point and then avg the clip hash for each point based on the KNN
            distances, indicies = self.k_nearest_sklearn(self.means.data, 3, True)
            distances = torch.from_numpy(distances).to(self.device)
            indicies = torch.from_numpy(indicies).to(self.device).view(-1)
            weights = torch.sigmoid(self.opacities[indicies].view(-1, 4))
            weights = torch.nn.Softmax(dim=-1)(weights)
            points = self.means[indicies]
            # clip_hash_encoding = self.gaussian_lerf_field.get_hash(self.means)
            clip_hash_encoding = self.gaussian_lerf_field.get_hash(points)
            clip_hash_encoding = clip_hash_encoding.view(-1, 4, clip_hash_encoding.shape[1])
            clip_hash_encoding = (clip_hash_encoding * weights.unsqueeze(-1))
            clip_hash_encoding = clip_hash_encoding.sum(dim=1)
            clip_feats = self.gaussian_lerf_field.get_outputs_from_feature(clip_hash_encoding, self.best_scales[0].to(self.device) * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32)
            relevancy = self.image_encoder.get_relevancy(clip_feats / (clip_feats.norm(dim=-1, keepdim=True)+1e-6), 0).view(self.num_points, -1)
            color = apply_colormap(relevancy[..., 0:1])
            self.viewer_control.viser_server.add_point_cloud("relevancy", self.means.numpy(force=True) * 10, color.numpy(force=True), 0.01)

            # Add a slider to debug the relevancy values
            
            # self.crop_ids = (relevancy[..., 0] > self.relevancy_thresh.value)
            
            #Define all crop viewer elements
            self.crop_points = relevancy[..., 0] > self.relevancy_thresh.value
            self._crop_center_init = self.means[self.crop_points].mean(dim=0).cpu().numpy()
            self.original_means = self.means.data.clone()

            self._crop_handle = self.viewer_control.viser_server.add_transform_controls("Crop Points", depth_test=False, line_width=4.0)
            world_center = tuple(p / self.viser_scale_ratio for p in self._crop_center_init)
            self._crop_handle.position = world_center

            @self._crop_handle.on_update
            def _update_crop_handle(han):
                # import pdb; pdb.set_trace()
                if self._crop_center_init is None:
                    return
                # import pdb; pdb.set_trace()
                new_center = np.array(self._crop_handle.position) * self.viser_scale_ratio
                delta = new_center - self._crop_center_init
                displacement = torch.zeros_like(self.means)
                displacement[self.crop_points] = torch.from_numpy(delta).to(self.device).to(self.means.dtype)
                
                curr_to_world = torch.from_numpy(vtf.SE3(np.concatenate((self._crop_handle.wxyz, self._crop_handle.position * self.viser_scale_ratio))).as_matrix()).to(self.device).to(self.means.dtype)
                transform = torch.from_numpy(vtf.SE3(np.concatenate((self._crop_handle.wxyz, (self._crop_handle.position * self.viser_scale_ratio) - self._crop_center_init))).as_matrix()).to(self.device).to(self.means.dtype)

                print(f"transform {transform}")
                transformed_points = self.original_means.clone()
                homogeneous_points = torch.cat((transformed_points[self.crop_points], torch.ones(transformed_points[self.crop_points].shape[0], 1, device=self.device, dtype=self.means.dtype)), dim=1)
                transformed_homogeneous = curr_to_world @ transform @ torch.inverse(curr_to_world) @ homogeneous_points.transpose(0,1)
                transformed_homogeneous = transformed_homogeneous.transpose(0,1)
                transformed_points[self.crop_points] = transformed_homogeneous[:, :3] / transformed_homogeneous[:, 3:4]
                self.means.data = transformed_points

            # self._crop_center.value = tuple(p / self.viser_scale_ratio for p in self._crop_center_init)

            self.viewer_control.viser_server.add_point_cloud("Centroid", self._crop_center_init / self.viser_scale_ratio, np.array([0,0,0]), 0.1)

    def reset_crop_cb(self,element):
        self.crop_ids = None#torch.ones_like(self.means[:,0],dtype=torch.bool)
        self.means.data = self.original_means
        self._crop_center_init = None
        self._crop_handle.visible = False
        
    def get_max_across(self, xys, depths, radii, conics, num_tiles_hit, opacities, h, w, preset_scales=None):
        # probably not a good idea bc it's prob going to be a lot of memory
        n_phrases = len(self.image_encoder.positives)
        n_phrases_maxs = [None for _ in range(n_phrases)]
        n_phrases_sims = [None for _ in range(n_phrases)]
        scales_list = torch.linspace(0.0, 1.5, 30).to(self.device)
        # scales_list = [0.1]
        all_probs = []
        BLOCK_WIDTH = 16

        with torch.no_grad():
            clip_hash_encoding = self.gaussian_lerf_field.get_hash(self.means)
            # print(type(clip_hash_encoding))
            # print(clip_hash_encoding.ndimension())
            # print(clip_hash_encoding.size(1))
            # import pdb; pdb.set_trace()
            clip_output = rasterize_gaussians(
                            xys,
                            depths,
                            radii,
                            conics,
                            num_tiles_hit,
                            # self.gaussian_lerf_field.get_outputs_from_feature(self.clip_hash / self.clip_hash.norm(dim=-1, keepdim=True), scale * torch.ones(h*w, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].view(self.num_points, -1).to(dtype=torch.float32),
                            # self.clip_hash / self.clip_hash.norm(dim=-1, keepdim=True),
                            # clip_hash_encoding / clip_hash_encoding.norm(dim=-1, keepdim=True),
                            clip_hash_encoding,
                            opacities,
                            h,
                            w,
                            BLOCK_WIDTH,
                            # torch.zeros(self.image_encoder.embedding_dim, device=self.device),
                            torch.zeros(clip_hash_encoding.shape[1], device=self.device),
                        )
            # Normalize the clip output
            # clip_output = clip_output / (clip_output.norm(dim=-1, keepdim=True) + 1e-6)
        for i, scale in enumerate(scales_list):
            with torch.no_grad():
                clip_output_im = self.gaussian_lerf_field.get_outputs_from_feature(clip_output.view(h*w, -1), scale * torch.ones(h*w, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].to(dtype=torch.float32).view(h, w, -1)

            for j in range(n_phrases):
                if preset_scales is None or j == i:
                    # relevancy_rasterized = NDRasterizeGaussians.apply(
                    #         xys,
                    #         depths,
                    #         radii,
                    #         conics,
                    #         num_tiles_hit,
                    #         self.image_encoder.get_relevancy(self.gaussian_lerf_field.get_outputs_from_feature(self.clip_hash / self.clip_hash.norm(dim=-1, keepdim=True), scale * torch.ones(self.num_points, 1, device=self.device))[GaussianLERFFieldHeadNames.CLIP].view(self.num_points, -1).to(dtype=torch.float32), j)[..., 0:1],
                    #         opacities,
                    #         h,
                    #         w,
                    #         torch.zeros(1, device=self.device),
                    #     )
                    
                    probs = self.image_encoder.get_relevancy(clip_output_im.view(-1, self.image_encoder.embedding_dim), j)
                    pos_prob = probs[..., 0:1]
                    all_probs.append((pos_prob.max(), scale))
                    if n_phrases_maxs[j] is None or pos_prob.max() > n_phrases_sims[j].max():
                        n_phrases_maxs[j] = scale
                        n_phrases_sims[j] = pos_prob
        # print(f"Best scales: {n_phrases_maxs}")#, Words: {self.image_encoder.positives}, Scale List: {scales_list}, All probs: {all_probs}")
        # import pdb; pdb.set_trace()
        return torch.stack(n_phrases_sims), torch.Tensor(n_phrases_maxs)#, relevancy_rasterized
