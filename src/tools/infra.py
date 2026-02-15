"""Infrastructure tools: get_connectivity, list_machines, add_machine, add_container."""

import json

from mcp.server.fastmcp import Context

from src.server import mcp


@mcp.tool()
async def get_connectivity(project: str, ctx: Context = None) -> str:
    """
    Get all connectivity info for a project: machines, containers, databases.

    Args:
        project: Project name
    """
    app = ctx.request_context.lifespan_context

    # Get containers for this project
    containers = await app.db.fetch(
        """
        SELECT c.*, m.name as machine_name, m.ip, m.ssh_command
        FROM containers c
        JOIN machines m ON c.machine_id = m.id
        WHERE c.project = $1
        """,
        project
    )

    # Get databases for this project
    databases = await app.db.fetch(
        """
        SELECT d.*, m.name as machine_name, m.ip
        FROM databases d
        JOIN machines m ON d.machine_id = m.id
        WHERE d.project = $1
        """,
        project
    )

    result = {
        "project": project,
        "containers": [
            {
                "name": c["name"],
                "machine": c["machine_name"],
                "ip": c["ip"],
                "ssh_command": c["ssh_command"],
                "compose_path": c["compose_path"],
                "ports": c["ports"],
                "status": c["status"]
            }
            for c in containers
        ],
        "databases": [
            {
                "name": d["name"],
                "type": d["db_type"],
                "machine": d["machine_name"],
                "connection_hint": d["connection_hint"]
            }
            for d in databases
        ]
    }

    return json.dumps(result)


@mcp.tool()
async def list_machines(ctx: Context = None) -> str:
    """List all registered machines with connection info."""
    app = ctx.request_context.lifespan_context

    rows = await app.db.fetch("SELECT * FROM machines ORDER BY name")

    machines = [
        {
            "name": r["name"],
            "ip": r["ip"],
            "ssh_command": r["ssh_command"],
            "notes": r["notes"]
        }
        for r in rows
    ]

    return json.dumps({"machines": machines})


@mcp.tool()
async def add_machine(
    name: str,
    ip: str = None,
    ssh_command: str = None,
    notes: str = None,
    ctx: Context = None
) -> str:
    """
    Register a new machine.

    Args:
        name: Machine identifier (e.g., 'mac-studio', 'slmbeast')
        ip: IP address (optional)
        ssh_command: SSH command to connect (optional)
        notes: Additional notes (optional)
    """
    app = ctx.request_context.lifespan_context

    row = await app.db.fetchrow(
        """
        INSERT INTO machines (name, ip, ssh_command, notes)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (name) DO UPDATE SET
            ip = COALESCE($2, machines.ip),
            ssh_command = COALESCE($3, machines.ssh_command),
            notes = COALESCE($4, machines.notes),
            updated_at = NOW()
        RETURNING id
        """,
        name, ip, ssh_command, notes
    )

    return json.dumps({"success": True, "machine_id": row["id"]})


@mcp.tool()
async def add_container(
    name: str,
    machine: str,
    project: str,
    ports: str = None,
    compose_path: str = None,
    ctx: Context = None
) -> str:
    """
    Register a Docker container.

    Args:
        name: Container name
        machine: Machine it runs on
        project: Associated project
        ports: Port mapping (e.g., '8001:8000')
        compose_path: Path to docker-compose.yml
    """
    app = ctx.request_context.lifespan_context

    # Get machine ID
    machine_row = await app.db.fetchrow("SELECT id FROM machines WHERE name = $1", machine)
    if not machine_row:
        return json.dumps({"error": f"Machine '{machine}' not found"})

    row = await app.db.fetchrow(
        """
        INSERT INTO containers (name, machine_id, project, ports, compose_path)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        name, machine_row["id"], project, ports, compose_path
    )

    return json.dumps({"success": True, "container_id": row["id"]})
