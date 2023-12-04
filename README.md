# Life-Long Language Embedded Gaussian Splats
<!--<div align='center'>
<img src="https://www.lerf.io/data/nerf_render.svg" height="230px">
</div>-->

# Installation
Life-Long Language Embedded Gaussian Splats follows the integration guidelines described [here](https://docs.nerf.studio/developer_guides/new_methods.html) for custom methods within Nerfstudio. 
### 0. Install Nerfstudio dependencies
[Follow these instructions](https://docs.nerf.studio/quickstart/installation.html) up to and including "tinycudann" to install dependencies.

 ***If you'll be using ROS messages do not use a conda environment and enter the dependency install commands below instead*** (ROS2 and conda don't play well together)

 ```
 pip install torch==2.0.1+cu118 torchvision==0.15.2+cu118 --extra-index-url https://download.pytorch.org/whl/cu118
 pip install ninja git+https://github.com/NVlabs/tiny-cuda-nn/#subdirectory=bindings/torch
 ```
### 1. Clone and install repo
```
git clone https://github.com/BerkeleyAutomation/L3GOS
cd L3GOS/l3gos/
python -m pip install -e .
ns-install-cli
```

### Checking the install
Run `ns-train -h`: you should see a list of "subcommands" with lllegos and llgs included among them.


- Launch training with `ns-train l3gos --data <data_folder>`. This specifies a data folder to use. For more details, see [Nerfstudio documentation](https://docs.nerf.studio/en/latest/quickstart/first_nerf.html). 
- Connect to the viewer by forwarding the viewer port (we use VSCode to do this), and click the link to `viewer.nerf.studio` provided in the output of the train script
- Within the viewer, you can type text into the textbox, then select the `relevancy_0` output type to visualize relevancy maps.

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