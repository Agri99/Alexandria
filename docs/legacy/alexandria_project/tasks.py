# src/alexandria_project/tasks.py
# Simple in‑memory task registry for future cancellation (placeholder for future extension)
import asyncio

_task_registry = {}

def register_task(task_id: str, task: asyncio.Task):
    _task_registry[task_id] = task

def cancel_task(task_id: str) -> bool:
    task = _task_registry.get(task_id)
    if task:
        task.cancel()
        return True
    return False

def unregister_task(task_id: str):
    _task_registry.pop(task_id, None)
