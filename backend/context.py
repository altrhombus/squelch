"""
Shared application singletons, assigned during the FastAPI lifespan.

Route modules access these at request time via `from .. import context`,
then `context.radio` etc.  Never `from ..context import radio` — that would
freeze the None value bound at import.
"""

config: dict = {}
meta = None       # MetadataState
radio = None      # RadioManager
recorder = None   # Recorder
streams = None    # StreamingManager
icecast = None    # IcecastPusher, only when enabled in config
