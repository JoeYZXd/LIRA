"""Call injected adapters without hiding errors raised inside their bodies."""
import inspect


def invoke_callback(callback, *args, **kwargs):
    parameters = inspect.signature(callback).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        kwargs = {key: value for key, value in kwargs.items() if key in parameters}
    return callback(*args, **kwargs)
