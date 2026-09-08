from comfy_api.latest import ComfyExtension, io

import os

from .nodes import DLSSNRVideoUpscale, DLSSNRImageUpscale, DLSSNRFrameInterp

WEB_DIRECTORY = "./web/js"

__all__ = ["comfy_entrypoint", "WEB_DIRECTORY"]


class DLSSNRExtension(ComfyExtension):
    async def on_load(self):
        pass

    async def get_node_list(self):
        return [DLSSNRVideoUpscale, DLSSNRFrameInterp, DLSSNRImageUpscale]


def comfy_entrypoint():
    return DLSSNRExtension()

