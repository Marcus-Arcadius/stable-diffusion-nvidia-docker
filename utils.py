from typing import Tuple, List
import torch
import torch.nn as nn
from PIL import Image
import numpy as np
import multiprocessing
from diffusers import StableDiffusionPipeline
from diffusers.pipelines.stable_diffusion.safety_checker import (
    StableDiffusionSafetyChecker,
)
from transformers import CLIPFeatureExtractor
from transformers.feature_extraction_utils import BatchFeature


def image_grid(imgs, rows, cols):
    assert len(imgs) == rows * cols

    w, h = imgs[0].size
    grid = Image.new("RGB", size=(cols * w, rows * h))
    grid_w, grid_h = grid.size

    for i, img in enumerate(imgs):
        grid.paste(img, box=(i % cols * w, i // cols * h))
    return grid


def dummy_checker(images, *args, **kwargs):
    # removes nsfw filter
    return images, False


def dummy_extractor(images, return_tensors="pt"):
    # print(type(images), type(images[0]))
    if type(images) is list:
        images = [np.array(img) for img in images]
    data = {"pixel_values": images}
    return BatchFeature(data=data, tensor_type=return_tensors)

    return torch.from_numpy(np.array(images))


def remove_nsfw(
    model: StableDiffusionPipeline,
) -> Tuple[StableDiffusionSafetyChecker, CLIPFeatureExtractor]:
    nsfw_model: StableDiffusionSafetyChecker = model.safety_checker
    model.safety_checker = dummy_checker
    extr = model.feature_extractor
    model.feature_extractor = dummy_extractor
    return nsfw_model.cpu(), extr


def get_gpu_setting(env_var: str) -> Tuple[bool, List[int]]:
    if not torch.cuda.is_available():
        print("GPU not detected! Make sure you have a GPU to reduce inference time!")
        return False, []
    # reads user input, returns multi_gpu flag and gpu id(s)
    n = torch.cuda.device_count()
    if env_var == "all":
        gpus = list(range(n))
    elif "," in env_var:
        gpus = [int(gnum) for gnum in env_var.split(",") if int(gnum) < n]
    else:
        gpus = [int(env_var)]
    assert len(
        gpus
    ), f"Make sure to provide valid device ids! You have {n} GPU(s), you can specify the following values: {list(range(n))}"
    return len(gpus) > 1, gpus


def get_free_memory_Mb(device: int):
    # returns (free, total) device memory, in bytes
    return torch.cuda.mem_get_info(device)[0] / 2 ** 20


def model_size_Mb(model):
    # from the legend @ptrblck himself https://discuss.pytorch.org/t/finding-model-size/130275/2
    param_size = 0
    for param in model.parameters():
        param_size += param.nelement() * param.element_size()
    buffer_size = 0
    for buffer in model.buffers():
        buffer_size += buffer.nelement() * buffer.element_size()
    return (param_size + buffer_size) / 1024 ** 2


class ToGPUWrapper(nn.Module, object):
    def __init__(self, layer: nn.Module, device: torch.device) -> None:
        # composition design, we wrap a nn.Module, change forward
        super().__init__()
        self.device = device
        # move wrapped model to correct device
        self.layer = layer.to(device)

    def forward(self, x: torch.Tensor, *args, **kwargs):
        # move input and output to given device
        # print(self.layer.__class__.__name__)
        args = [a.to(self.device) if type(a) is torch.Tensor else a for a in args]
        for k in kwargs:
            if type(kwargs[k]) is torch.Tensor:
                kwargs[k] = kwargs[k].to(self.device)

        y = self.layer(x.to(self.device), *args, **kwargs)
        # text model wraps output.. this could be made more generic
        if self.layer.__class__.__name__ == "CLIPTextModel":
            # getting does something like this self.to_tuple()[k]
            y.last_hidden_state = y.last_hidden_state.to(self.device)
            return y
        return y.to(self.device)

    # FIXME this is giving recursion problems
    # def __getattr__(self, name: str):
    # return getattr(self.layer, name)

    def __iter__(self):
        return iter(self.layer)

    def __next__(self):
        return next(self.layer)

    def decode(self, z):
        # for vae output
        return self.layer.decode(z.to(self.device))


class ModelParts2GPUsAssigner:
    def __init__(self, devices: List[int],) -> None:
        self.N = len(devices)
        # memory "budget" for each device: we consider 80% of the available GPU memory
        # so that the rest can be used for storing intermediate results
        # TODO unet uses way more than the other components, balance that out
        G = [int(get_free_memory_Mb(d) * 0.6) for d in devices]
        print("Free GPU memory", G)
        # FIXME G is kind of a function of n_models itself, as the more models you have
        # the more memory you will be using for storing intermediate results...
        self.G = np.array(G, dtype=np.uint16)
        # model components memory usage, fixed order: unet_e, unet_d, text_encoder, vae
        # TODO make dynamic using `model_size_Mb(model.text_encoder)`,
        # BUG this wont work for fp32, sizes are bigger
        self.W = np.array([666, 975, 235, 160])

        # max number of models you can have considering pooled VRam as it if was a single GPU,
        # "upper bounded" by max number of processes
        # TODO should we make it easier to allow single model multiple gpus?
        self._max_models = min(
            multiprocessing.cpu_count(), np.floor(self.G.sum() / self.W.sum())
        )
        if self._max_models == 0:
            raise Exception(
                "You don't have enough combined VRam to host a single model!"
            )

    def state_evaluation(self, state: np.ndarray):
        """
        2 conditions:
            - each model component must appear in the same number (implicitly generated)
            - allocation on each GPUs must not be greater than its capacity
        """
        return (state @ self.W <= self.G).all()

    def add_model(self, state: np.ndarray, rnd=True, sample_size=2):
        # TODO proper docs
        def get_device_permutation():
            if rnd:
                return np.random.permutation(self.N)
            return np.arange(self.N)

        # beware, this will modify state in-place
        valid = []
        # N^4 possible combinations
        # plus one on cells (0, a), (1, b), (2, c), (3, d)
        for a in get_device_permutation():
            state[a, 0] += 1
            for b in get_device_permutation():
                state[b, 1] += 1
                for c in get_device_permutation():
                    state[c, 2] += 1
                    for d in get_device_permutation():
                        state[d, 3] += 1
                        # evaluate state, return first valid or keep a list of valid ones? Or one with max "score"?
                        # greedy return one, can't guarantee to find (one of the) optimum(s)
                        if self.state_evaluation(state):
                            # could be compressed by only storing a,b,c,d..
                            valid.append(state.copy())
                        # here state wasn't backtracked!
                        if sample_size > 0 and len(valid) >= sample_size:
                            return valid
                        # backtrack!
                        state[d, 3] -= 1
                    state[c, 2] -= 1
                state[b, 1] -= 1
            state[a, 0] -= 1
        return valid

    def find_best_assignment(
        self, state: np.ndarray, curr_n_models: int, **kwargs
    ) -> Tuple[int, List[np.ndarray]]:
        # try to increase the number of models until you can have no more, recursively
        if curr_n_models >= self._max_models:
            return -1, []
        prev = state.copy()
        valid = self.add_model(state, **kwargs)
        # can't generate valid assignments with an extra model, return current one
        if not len(valid):
            return curr_n_models, [prev]
        # visit children
        children = []
        for next_state in valid:
            # insert only valid states
            depth, ss = self.find_best_assignment(
                next_state, curr_n_models + 1, **kwargs
            )
            if depth > 0 and len(ss):
                children.append((depth, ss))

        # can't add more models
        if not len(children):
            return curr_n_models + 1, valid
        # return best child, the one that assigns more models
        return max(children, key=lambda t: t[0])

    def __call__(self) -> np.ndarray:
        # initial empty assignment, #GPUs x #model_parts
        I = np.zeros((self.N, 4), dtype=np.uint16)
        # returns a valid assignment of split component to devices
        n_models, ass = self.find_best_assignment(I, 0)
        ass = ass[0]
        print(
            f"Search has found that {n_models} model(s) can be split over {self.N} device(s)!"
        )
        # format output into a [{model_component->device}], one per model to create
        model_ass = [{i: -1 for i in range(4)} for _ in range(n_models)]
        for comp in range(4):
            for dev in range(self.N):
                # this might say "component_0 to device_1 3 times"
                for _ in range(ass[dev, comp]):
                    for m in model_ass:
                        # assign to model that doesn't have an allocated component yet
                        if m[comp] == -1:
                            m[comp] = dev
        return model_ass
