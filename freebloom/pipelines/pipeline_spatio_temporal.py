# Adapted from https://github.com/huggingface/diffusers/blob/main/src/diffusers/pipelines/stable_diffusion/pipeline_stable_diffusion.py
import copy
import inspect
import os.path
from dataclasses import dataclass
from typing import Callable, List, Optional, Union

import numpy as np
import torch
import torchvision.utils
from diffusers.configuration_utils import FrozenDict
from diffusers.models import AutoencoderKL
from diffusers.pipeline_utils import DiffusionPipeline
from diffusers.schedulers import (
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    LMSDiscreteScheduler,
    PNDMScheduler,
)
from diffusers.utils import deprecate, logging, BaseOutput
from diffusers.utils import is_accelerate_available
from einops import rearrange, repeat
from packaging import version
from transformers import CLIPTextModel, CLIPTokenizer

from ..models.unet import UNet3DConditionModel
from ..prompt_attention import attention_util, ptp_utils

logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


@dataclass
class SpatioTemporalPipelineOutput(BaseOutput):
    videos: Union[torch.Tensor, np.ndarray]


class SpatioTemporalPipeline(DiffusionPipeline):
    _optional_components = []

    def __init__(
            self,
            vae: AutoencoderKL,
            text_encoder: CLIPTextModel,
            tokenizer: CLIPTokenizer,
            unet: UNet3DConditionModel,
            scheduler: Union[
                DDIMScheduler,
                PNDMScheduler,
                LMSDiscreteScheduler,
                EulerDiscreteScheduler,
                EulerAncestralDiscreteScheduler,
                DPMSolverMultistepScheduler,
            ],
            noise_scheduler: Union[
                DDIMScheduler,
                PNDMScheduler,
                LMSDiscreteScheduler,
                EulerDiscreteScheduler,
                EulerAncestralDiscreteScheduler,
                DPMSolverMultistepScheduler,
            ] = None,
            disk_store: bool = False,
            config=None
    ):
        super().__init__()

        if hasattr(scheduler.config, "steps_offset") and scheduler.config.steps_offset != 1:
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} is outdated. `steps_offset`"
                f" should be set to 1 instead of {scheduler.config.steps_offset}. Please make sure "
                "to update the config accordingly as leaving `steps_offset` might led to incorrect results"
                " in future versions. If you have downloaded this checkpoint from the Hugging Face Hub,"
                " it would be very nice if you could open a Pull request for the `scheduler/scheduler_config.json`"
                " file"
            )
            deprecate("steps_offset!=1", "1.0.0", deprecation_message, standard_warn=False)
            new_config = dict(scheduler.config)
            new_config["steps_offset"] = 1
            scheduler._internal_dict = FrozenDict(new_config)

        if hasattr(scheduler.config, "clip_sample") and scheduler.config.clip_sample is True:
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} has not set the configuration `clip_sample`."
                " `clip_sample` should be set to False in the configuration file. Please make sure to update the"
                " config accordingly as not setting `clip_sample` in the config might lead to incorrect results in"
                " future versions. If you have downloaded this checkpoint from the Hugging Face Hub, it would be very"
                " nice if you could open a Pull request for the `scheduler/scheduler_config.json` file"
            )
            deprecate("clip_sample not set", "1.0.0", deprecation_message, standard_warn=False)
            new_config = dict(scheduler.config)
            new_config["clip_sample"] = False
            scheduler._internal_dict = FrozenDict(new_config)

        is_unet_version_less_0_9_0 = hasattr(unet.config, "_diffusers_version") and version.parse(
            version.parse(unet.config._diffusers_version).base_version
        ) < version.parse("0.9.0.dev0")
        is_unet_sample_size_less_64 = hasattr(unet.config, "sample_size") and unet.config.sample_size < 64
        if is_unet_version_less_0_9_0 and is_unet_sample_size_less_64:
            deprecation_message = (
                "The configuration file of the unet has set the default `sample_size` to smaller than"
                " 64 which seems highly unlikely. If your checkpoint is a fine-tuned version of any of the"
                " following: \n- CompVis/stable-diffusion-v1-4 \n- CompVis/stable-diffusion-v1-3 \n-"
                " CompVis/stable-diffusion-v1-2 \n- CompVis/stable-diffusion-v1-1 \n- runwayml/stable-diffusion-v1-5"
                " \n- runwayml/stable-diffusion-inpainting \n you should change 'sample_size' to 64 in the"
                " configuration file. Please make sure to update the config accordingly as leaving `sample_size=32`"
                " in the config might lead to incorrect results in future versions. If you have downloaded this"
                " checkpoint from the Hugging Face Hub, it would be very nice if you could open a Pull request for"
                " the `unet/config.json` file"
            )
            deprecate("sample_size<64", "1.0.0", deprecation_message, standard_warn=False)
            new_config = dict(unet.config)
            new_config["sample_size"] = 64
            unet._internal_dict = FrozenDict(new_config)

        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler,
            noise_scheduler=noise_scheduler
        )
        self.vae_scale_factor = 2 ** (len(self.vae.config.block_out_channels) - 1)

        self.controller = attention_util.AttentionTest(disk_store=disk_store, config=config)
        self.hyper_config = config

    def enable_vae_slicing(self):
        self.vae.enable_slicing()

    def disable_vae_slicing(self):
        self.vae.disable_slicing()

    def enable_sequential_cpu_offload(self, gpu_id=0):
        if is_accelerate_available():
            from accelerate import cpu_offload
        else:
            raise ImportError("Please install accelerate via `pip install accelerate`")

        device = torch.device(f"cuda:{gpu_id}")

        for cpu_offloaded_model in [self.unet, self.text_encoder, self.vae]:
            if cpu_offloaded_model is not None:
                cpu_offload(cpu_offloaded_model, device)

    @property
    def _execution_device(self):
        if self.device != torch.device("meta") or not hasattr(self.unet, "_hf_hook"):
            return self.device
        for module in self.unet.modules():
            if (
                    hasattr(module, "_hf_hook")
                    and hasattr(module._hf_hook, "execution_device")
                    and module._hf_hook.execution_device is not None
            ):
                return torch.device(module._hf_hook.execution_device)
        return self.device

    def _encode_prompt(self, prompt, device, num_videos_per_prompt, do_classifier_free_guidance, negative_prompt):
        batch_size = len(prompt) if isinstance(prompt, list) else 1

        text_inputs = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt",
        )
        text_input_ids = text_inputs.input_ids
        untruncated_ids = self.tokenizer(prompt, padding="longest", return_tensors="pt").input_ids

        if untruncated_ids.shape[-1] >= text_input_ids.shape[-1] and not torch.equal(text_input_ids, untruncated_ids):
            removed_text = self.tokenizer.batch_decode(untruncated_ids[:, self.tokenizer.model_max_length - 1: -1])
            logger.warning(
                "The following part of your input was truncated because CLIP can only handle sequences up to"
                f" {self.tokenizer.model_max_length} tokens: {removed_text}"
            )

        if hasattr(self.text_encoder.config, "use_attention_mask") and self.text_encoder.config.use_attention_mask:
            attention_mask = text_inputs.attention_mask.to(device)
        else:
            attention_mask = None

        text_embeddings = self.text_encoder(
            text_input_ids.to(device),
            attention_mask=attention_mask,
        )
        text_embeddings = text_embeddings[0]

        # duplicate text embeddings for each generation per prompt, using mps friendly method
        bs_embed, seq_len, _ = text_embeddings.shape
        text_embeddings = text_embeddings.repeat(1, num_videos_per_prompt, 1)
        text_embeddings = text_embeddings.view(bs_embed * num_videos_per_prompt, seq_len, -1)

        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance:
            uncond_tokens: List[str]
            if negative_prompt is None:
                uncond_tokens = [""] * batch_size
            elif type(prompt) is not type(negative_prompt):
                raise TypeError(
                    f"`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} !="
                    f" {type(prompt)}."
                )
            elif isinstance(negative_prompt, str):
                uncond_tokens = [negative_prompt]
            elif batch_size != len(negative_prompt):
                raise ValueError(
                    f"`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`:"
                    f" {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches"
                    " the batch size of `prompt`."
                )
            else:
                uncond_tokens = negative_prompt

            max_length = text_input_ids.shape[-1]
            uncond_input = self.tokenizer(
                uncond_tokens,
                padding="max_length",
                max_length=max_length,
                truncation=True,
                return_tensors="pt",
            )

            if hasattr(self.text_encoder.config, "use_attention_mask") and self.text_encoder.config.use_attention_mask:
                attention_mask = uncond_input.attention_mask.to(device)
            else:
                attention_mask = None

            uncond_embeddings = self.text_encoder(
                uncond_input.input_ids.to(device),
                attention_mask=attention_mask,
            )
            uncond_embeddings = uncond_embeddings[0]

            # duplicate unconditional embeddings for each generation per prompt, using mps friendly method
            seq_len = uncond_embeddings.shape[1]
            uncond_embeddings = uncond_embeddings.repeat(1, num_videos_per_prompt, 1)
            uncond_embeddings = uncond_embeddings.view(batch_size * num_videos_per_prompt, seq_len, -1)

            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

        return text_embeddings

    def decode_latents(self, latents, return_tensor=False):
        video_length = latents.shape[2]
        latents = 1 / 0.18215 * latents
        latents = rearrange(latents, "b c f h w -> (b f) c h w")
        video = self.vae.decode(latents).sample
        video = rearrange(video, "(b f) c h w -> b c f h w", f=video_length)
        video = (video / 2 + 0.5).clamp(0, 1)
        if return_tensor:
            return video
        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloa16
        video = video.cpu().float().numpy()
        return video

    def prepare_extra_step_kwargs(self, generator, eta):
        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]

        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        # check if the scheduler accepts generator
        accepts_generator = "generator" in set(inspect.signature(self.scheduler.step).parameters.keys())
        if accepts_generator:
            extra_step_kwargs["generator"] = generator
        return extra_step_kwargs

    def check_inputs(self, prompt, height, width, callback_steps, latents):
        if not isinstance(prompt, str) and not isinstance(prompt, list):
            raise ValueError(f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")

        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

        if (callback_steps is None) or (
                callback_steps is not None and (not isinstance(callback_steps, int) or callback_steps <= 0)
        ):
            raise ValueError(
                f"`callback_steps` has to be a positive integer but is {callback_steps} of type"
                f" {type(callback_steps)}."
            )
        if isinstance(prompt, list) and latents is not None and len(prompt) != latents.shape[2]:
            raise ValueError(
                f"`prompt` is list but does match all frames. frames length: {latents.shape[2]}, prompt length: {len(prompt)}")

    def prepare_latents(self, batch_size, num_channels_latents, video_length, height, width, dtype, device, generator,
                        latents=None, store_attention=True, frame_same_noise=False):
        if store_attention and self.controller is not None:
            ptp_utils.register_attention_control(self, self.controller)

        shape = (
            batch_size, num_channels_latents, video_length, height // self.vae_scale_factor,
            width // self.vae_scale_factor)
        if frame_same_noise:
            shape = (batch_size, num_channels_latents, 1, height // self.vae_scale_factor,
                     width // self.vae_scale_factor)
        if isinstance(generator, list) and len(generator) != batch_size:
            raise ValueError(
                f"You have passed a list of generators of length {len(generator)}, but requested an effective batch"
                f" size of {batch_size}. Make sure the batch size matches the length of the generators."
            )

        if latents is None:
            rand_device = "cpu" if device.type == "mps" else device

            if isinstance(generator, list):
                shape = (1,) + shape[1:]
                latents = [
                    torch.randn(shape, generator=generator[i], device=rand_device, dtype=dtype)
                    for i in range(batch_size)
                ]
                if frame_same_noise:
                    latents = [repeat(latent, 'b c 1 h w -> b c f h w', f=video_length) for latent in latents]
                latents = torch.cat(latents, dim=0).to(device)
            else:
                latents = torch.randn(shape, generator=generator, device=rand_device, dtype=dtype).to(device)
                if frame_same_noise:
                    latents = repeat(latents, 'b c 1 h w -> b c f h w', f=video_length)

        else:
            if latents.shape != shape:
                raise ValueError(f"Unexpected latents shape, got {latents.shape}, expected {shape}")
            latents = latents.to(device)

        # scale the initial noise by the standard deviation required by the scheduler
        latents = latents * self.scheduler.init_noise_sigma
        return latents

    @torch.no_grad()
    def __call__(
            self,
            prompt: Union[str, List[str]],
            video_length: Optional[int],
            height: Optional[int] = None,
            width: Optional[int] = None,
            num_inference_steps: int = 50,
            guidance_scale: float = 7.5,
            negative_prompt: Optional[Union[str, List[str]]] = None,
            num_videos_per_prompt: Optional[int] = 1,
            eta: float = 0.0,
            generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
            latents: Optional[torch.FloatTensor] = None,
            output_type: Optional[str] = "tensor",
            return_dict: bool = True,
            callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
            callback_steps: Optional[int] = 1,
            fixed_latents: Optional[dict[torch.FloatTensor]] = None,
            fixed_latents_idx: list = None,
            inner_idx: list = None,
            init_text_embedding: torch.Tensor = None,
            mask: Optional[dict] = None,
            save_vis_inner=False,
            return_tensor=False,
            return_text_embedding=False,
            output_dir=None,
            **kwargs,
    ):
        if self.controller is not None:
            self.controller.reset()
            self.controller.batch_size = video_length

        # Default height and width to unet
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        # Check inputs. Raise error if not correct
        self.check_inputs(prompt, height, width, callback_steps, latents)

        # Define call parameters
        # batch_size = 1 if isinstance(prompt, str) else len(prompt)
        batch_size = 1
        device = self._execution_device
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0

        # Encode input prompt
        text_embeddings = self._encode_prompt(
            prompt, device, num_videos_per_prompt, do_classifier_free_guidance, negative_prompt
        )

        if init_text_embedding is not None:
            text_embeddings[video_length:] = init_text_embedding

        # Prepare timesteps
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = self.scheduler.timesteps

        # Prepare latent variables
        num_channels_latents = self.unet.in_channels
        latents = self.prepare_latents(
            batch_size * num_videos_per_prompt,
            num_channels_latents,
            video_length,
            height,
            width,
            text_embeddings.dtype,
            device,
            generator,
            latents,
        )
        latents_dtype = latents.dtype

        # Prepare extra step kwargs.
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)

        # Denoising loop
        num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                # expand the latents if we are doing classifier free guidance
                latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
                latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)

                # predict the noise residual
                noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample.to(
                    dtype=latents_dtype)

                # perform guidance
                if do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                    noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

                latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs).prev_sample

                if mask is not None and i < 10:
                    f_1 = latents[:, :, :1]
                    latents = mask[1] * f_1 + mask[0] * latents
                    latents = repeat(latents, 'b c 1 h w -> b c f h w', f=video_length)
                    latents = latents.to(latents_dtype)

                # call the callback, if provided
                if i == len(timesteps) - 1 or ((i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0):
                    progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        callback(i, t, latents)

                if self.controller is not None:
                    latents_old = latents
                    dtype = latents.dtype
                    latents_new = self.controller.step_callback(latents, inner_idx)
                    latents = latents_new.to(dtype)

                if fixed_latents is not None:
                    latents[:, :, fixed_latents_idx] = fixed_latents[i + 1]

                self.controller.empty_cache()

                if save_vis_inner:
                    video = self.decode_latents(latents)
                    video = torch.from_numpy(video)
                    video = rearrange(video, 'b c f h w -> (b f) c h w')
                    if os.path.exists(output_dir):
                        os.makedirs(f'{output_dir}/inner', exist_ok=True)
                        torchvision.utils.save_image(video, f'{output_dir}/inner/{i}.jpg')

        # Post-processing
        video = self.decode_latents(latents, return_tensor)

        # Convert to tensor
        if output_type == "tensor" and isinstance(video, np.ndarray):
            video = torch.from_numpy(video)

        if not return_dict:
            return video
        if return_text_embedding:
            return SpatioTemporalPipelineOutput(videos=video), text_embeddings[video_length:]

        return SpatioTemporalPipelineOutput(videos=video)

    def forward_to_t(self, latents_0, t, noise, text_embeddings):
        noisy_latents = self.noise_scheduler.add_noise(latents_0, noise, t)

        # Predict the noise residual and compute loss
        model_pred = self.unet(noisy_latents, t, text_embeddings).sample
        return model_pred

    def interpolate_between_two_frames(
            self,
            samples,
            x_noise,
            rand_ratio,
            where,
            prompt: Union[str, List[str]],
            k=3,
            height: Optional[int] = None,
            width: Optional[int] = None,
            num_inference_steps: int = 50,
            guidance_scale: float = 7.5,
            negative_prompt: Optional[Union[str, List[str]]] = None,
            num_videos_per_prompt: Optional[int] = 1,
            eta: float = 0.0,
            generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
            output_type: Optional[str] = "tensor",
            return_dict: bool = True,
            callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
            callback_steps: Optional[int] = 1,
            base_noise=None,
            text_embedding=None,
            **kwargs):
        if text_embedding is None:
            text_embeddings = self._encode_prompt(prompt, x_noise.device, num_videos_per_prompt, 0, None)
        else:
            text_embeddings = text_embedding
        prompt_temp = copy.deepcopy(prompt)

        times = 0
        k = k
        fixed_original_idx = where
        while times < k:
            if not isinstance(samples, torch.Tensor):
                samples = samples[0]

            inner_step = self.controller.latents_store
            if times == 0:
                for key, value in inner_step.items():
                    inner_step[key] = value[:, :, fixed_original_idx]

            b, c, f, _, __ = samples.shape
            dist_list = []
            for i in range(f - 1):
                dist_list.append(torch.nn.functional.mse_loss(samples[:, :, i + 1], samples[:, :, i]))
            dist = torch.tensor(dist_list)
            num_insert = 1
            value, idx = torch.topk(dist, k=num_insert)
            tgt_video_length = num_insert + f

            idx = sorted(idx)
            tgt_idx = [id + 1 + i for i, id in enumerate(idx)]

            original_idx = [i for i in range(tgt_video_length) if i not in tgt_idx]
            prompt_after_inner = []
            x_temp = []
            text_embeddings_temp = []
            prompt_i = 0
            for i in range(tgt_video_length):
                if i in tgt_idx:
                    prompt_after_inner.append('')
                    search_idx = sorted(original_idx + [i])
                    find = search_idx.index(i)
                    pre_idx, next_idx = search_idx[find - 1], search_idx[find + 1]
                    length = next_idx - pre_idx

                    x_temp.append(np.cos(rand_ratio * np.pi / 2) * base_noise[:, :, 0] + np.sin(
                        rand_ratio * np.pi / 2) * torch.randn_like(base_noise[:, :, 0]))
                    text_embeddings_temp.append(
                        (next_idx - i) / length * text_embeddings[prompt_i - 1] + (i - pre_idx) / length *
                        text_embeddings[prompt_i])
                else:
                    prompt_after_inner.append(prompt_temp[prompt_i])
                    x_temp.append(x_noise[:, :, prompt_i])
                    text_embeddings_temp.append(text_embeddings[prompt_i])
                    prompt_i += 1

            inner_idx = tgt_idx
            prompt_temp = prompt_after_inner

            x_noise = torch.stack(x_temp, dim=2)
            text_embeddings = torch.stack(text_embeddings_temp, dim=0)

            samples, prompt_embedding = self(prompt_temp,
                                             tgt_video_length,
                                             height,
                                             width,
                                             num_inference_steps,
                                             guidance_scale,
                                             [negative_prompt] * tgt_video_length,
                                             num_videos_per_prompt,
                                             eta,
                                             generator,
                                             x_noise,
                                             output_type,
                                             return_dict,
                                             callback,
                                             callback_steps,
                                             init_text_embedding=text_embeddings,
                                             inner_idx=inner_idx,
                                             return_text_embedding=True,
                                             fixed_latents=copy.deepcopy(inner_step),
                                             fixed_latents_idx=original_idx,
                                             **kwargs)

            times += 1

        return samples[0]
