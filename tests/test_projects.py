import asyncio

from database.db import (
    init_db,
    get_or_create_user,
)
from projects.service import (
    create,
    list_projects,
    open_project,
    remove_project,
)


async def main():
    await init_db()

    user_id = await get_or_create_user(
        telegram_id=999999993,
        username="project_test",
    )

    await remove_project(
        user_id,
        "kasper-test",
    )

    project_id = await create(
        user_id,
        "kasper-test",
        "Тестовый проект Kasper AI",
    )

    print("Created project:", project_id)

    projects = await list_projects(user_id)

    print("Projects:", len(projects))

    project = await open_project(
        user_id,
        "kasper-test",
    )

    print("Opened:", project)

    deleted = await remove_project(
        user_id,
        "kasper-test",
    )

    print("Deleted:", deleted)

    print("Projects service: OK")


asyncio.run(main())
