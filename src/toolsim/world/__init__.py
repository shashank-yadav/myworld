"""The world runtime: snapshot, fork, replay, mutate and evaluate any agent environment.

A world is a set of components (simulated services, directories, databases, remote processes in
any language), a journal of everything that happened to them, and checkpoints. See ``world.World``,
``component.Component`` (the six methods a component implements) and ``adapters`` (directory,
SQLite, the remote HTTP protocol).
"""

from .adapters import DirectoryComponent, RemoteComponent, SQLiteComponent, build, serve_component
from .component import Component, ServiceComponent, diff
from .world import World, result_sha

__all__ = ["Component", "DirectoryComponent", "RemoteComponent", "SQLiteComponent", "ServiceComponent", "World",
           "build", "diff", "result_sha", "serve_component"]
