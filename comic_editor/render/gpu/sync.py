"""Typed GL sync entry points (Qt's generated pointer overloads vary)."""
import ctypes


class GlSync:
    def __init__(self, context):
        call = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE)
        def function(name, result, *args):
            address = context.getProcAddress(name.encode('ascii'))
            if not address:
                raise RuntimeError(f'{name} is unavailable')
            return call(result, *args)(address)
        self.fence = function('glFenceSync', ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint)
        self.wait = function('glClientWaitSync', ctypes.c_uint, ctypes.c_void_p,
                             ctypes.c_uint, ctypes.c_uint64)
        self.delete = function('glDeleteSync', None, ctypes.c_void_p)

    def insert(self):
        result = self.fence(0x9117, 0)  # GL_SYNC_GPU_COMMANDS_COMPLETE
        if not result:
            raise RuntimeError('Could not fence a graphics image')
        return int(result)

    def ready(self, fence):
        if not fence:
            return True
        result = self.wait(fence, 0, 0)
        if result == 0x911D:  # GL_WAIT_FAILED
            raise RuntimeError('Graphics image fence failed')
        return result in (0x911A, 0x911C)  # ALREADY_SIGNALED, CONDITION_SATISFIED
