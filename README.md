# Language Embedded Gaussian Splats (LEGS)
<div align="center">

[[Website]](https://berkeleyautomation.github.io/LEGS/)
[[PDF]](https://autolab.berkeley.edu/assets/publications/media/2024_IROS_LEGS_CR.pdf)
[[Arxiv]](https://arxiv.org/abs/2409.18108)

<!-- insert figure -->
<!-- <img src="assets/joint.gif" width="600px"/> -->
[![Kitchen Queries](media/KitchenQueries.gif)](https://youtu.be/SubSWU1wJak)

[![Grocery Store Queries](media/GroceryStoreQueries.gif)](https://youtu.be/NA3m16Cgdm4)

</div>

This repository contains the code for the paper "Language-Embedded Gaussian Splats (LEGS): Incrementally Building Room-Scale Representations with a Mobile Robot".


# Installation
Language Embedded Gaussian Splats follows the integration guidelines described [here](https://docs.nerf.studio/developer_guides/new_methods.html) for custom methods within Nerfstudio.

To learn more about the code we use to interface with the robot and collect image poses, see this repo [here](https://github.com/BerkeleyAutomation/legs_ros_ws), which outlines our ROS2 interface.
### 0. Install Nerfstudio dependencies
[Follow these instructions](https://docs.nerf.studio/quickstart/installation.html) up to and including "tinycudann" to install dependencies.

 ***If you'll be using ROS messages do not use a conda environment and enter the dependency install commands below instead*** (ROS and conda don't play well together)

 ```
 pip install torch==2.0.1+cu118 torchvision==0.15.2+cu118 --extra-index-url https://download.pytorch.org/whl/cu118
 pip install ninja git+https://github.com/NVlabs/tiny-cuda-nn/#subdirectory=bindings/torch
 ```
### 1. Clone and install repo
```
git clone https://github.com/BerkeleyAutomation/L3GS
cd L3GS/l3gs/
python -m pip install -e .
ns-install-cli
```

### Checking the install
Run `ns-train -h`: you should see a list of "subcommands" with lllegos and llgs included among them.


- Launch training with `ns-train l3gs` and start publishing an imagepose topic or playing an imagepose ROS bag. 
- Connect to the viewer by forwarding the viewer port (we use VSCode to do this), and click the link to `viewer.nerf.studio` provided in the output of the train script

<!--
## Relevancy Map Normalization
By default, the viewer shows **raw** relevancy scaled with the turbo colormap. As values lower than 0.5 correspond to irrelevant regions, **we recommend setting the `range` parameter to (-1.0, 1.0)**. To match the visualization from the paper, check the `Normalize` tick-box, which stretches the values to use the full colormap.

The images below show the rgb, raw, centered, and normalized output views for the query "Lily".


<div align='center'>
<img src="readme_images/lily_rgb.jpg" width="150px">
<img src="readme_images/lily_raw.jpg" width="150px">
<img src="readme_images/lily_centered.jpg" width="150px">
<img src="readme_images/lily_normalized.jpg" width="150px">
</div>


# Extending LERF
Be mindful that code for visualization will change as more features are integrated into Nerfstudio, so if you fork this repo and build off of it, check back regularly for extra changes.
### Issues
Please open Github issues for any installation/usage problems you run into. We've tried to support as broad a range of GPUs as possible with `lerf-lite`, but it might be necessary to provide even more low-footprint versions. Thank you!
#### Known TODOs
- [ ] Integrate into `ns-render` commands to render videos from the command line with custom prompts
### Using custom image encoders
We've designed the code to modularly accept any image encoder that implements the interface in `BaseImageEncoder` (`image_encoder.py`). An example of different encoder implementations can be seen in `clip_encoder.py` vs `openclip_encoder.py`, which implement OpenAI's CLIP and OpenCLIP respectively.
### Code structure
(TODO expand this section)
The main file to look at for editing and building off LERF is `lerf.py`, which extends the Nerfacto model from Nerfstudio, adds an additional language field, losses, and visualization. The CLIP and DINO pre-processing are carried out by `pyramid_interpolator.py` and `dino_dataloader.py`.

## Bibtex
If you find this useful, please cite the paper!
<pre id="codecell0">@inproceedings{lerf2023,
&nbsp;author = {Kerr, Justin and Kim, Chung Min and Goldberg, Ken and Kanazawa, Angjoo and Tancik, Matthew},
&nbsp;title = {LERF: Language Embedded Radiance Fields},
&nbsp;booktitle = {International Conference on Computer Vision (ICCV)},
&nbsp;year = {2023},
} </pre>
-->
## Bibtex
If you find this useful, please cite the paper!
<pre id="codecell0">@article{yu2024language,
&nbsp;author = {Yu, Justin and Hari, Kush and Srinivas, Kishore and El-Refai, Karim and Rashid, Adam and Kim, Chung Min and Kerr, Justin and Cheng, Richard and Irshad, Muhammad Zubair and Balakrishna, Ashwin and Kollar, Thomas and Goldberg, Ken},
&nbsp;title = {Language-Embedded Gaussian Splats (LEGS): Incrementally Building Room-Scale Representations with a Mobile Robot},
&nbsp;booktitle = {International Conference on Intelligent Robots and Systems (IROS)},
&nbsp;year = {2024},
} </pre>
