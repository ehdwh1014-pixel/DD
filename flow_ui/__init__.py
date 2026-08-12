"""Pulse flow feedback control package."""

__all__ = ["FlowControlApp", "main"]


def __getattr__(name: str):
    if name in {"FlowControlApp", "main"}:
        from .app import FlowControlApp, main

        return {"FlowControlApp": FlowControlApp, "main": main}[name]
    raise AttributeError(name)
