from database.db import (
    create_project,
    get_projects,
    get_project,
    delete_project,
)


async def create(user_id, name, description=""):
    return await create_project(
        user_id,
        name,
        description,
    )


async def list_projects(user_id):
    return await get_projects(user_id)


async def open_project(user_id, name):
    return await get_project(
        user_id,
        name,
    )


async def remove_project(user_id, name):
    return await delete_project(
        user_id,
        name,
    )
