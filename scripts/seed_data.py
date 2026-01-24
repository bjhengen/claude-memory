#!/usr/bin/env python3
"""
Seed the Claude Memory database with initial data from existing knowledge.
Run this after deploying to populate the database.

Usage:
    python scripts/seed_data.py
"""

import os
import json
import asyncio
import asyncpg
from openai import AsyncOpenAI

# Configuration
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://claude:claude_memory_secret@localhost:5433/claude_memory"
)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


async def get_embedding(client: AsyncOpenAI, text: str) -> list[float]:
    """Generate embedding for text."""
    response = await client.embeddings.create(
        model="text-embedding-ada-002",
        input=text[:8000]  # Truncate to avoid token limits
    )
    return response.data[0].embedding


def format_embedding(embedding: list[float]) -> str:
    """Format embedding as PostgreSQL vector string."""
    return f"[{','.join(str(x) for x in embedding)}]"


async def seed_machines(pool: asyncpg.Pool):
    """Seed machines table."""
    machines = [
        {
            "name": "mac-studio",
            "ip": None,
            "ssh_command": None,
            "notes": "Primary development machine (M1 Pro)"
        },
        {
            "name": "work-laptop",
            "ip": None,
            "ssh_command": None,
            "notes": "Work laptop for remote development"
        },
        {
            "name": "slmbeast",
            "ip": "<YOUR_LOCAL_SERVER_IP>",
            "ssh_command": "ssh slmbeast",
            "notes": "Local AI server running Ollama with Qwen2.5:32b"
        },
        {
            "name": "aws-ec2",
            "ip": "<YOUR_EC2_IP>",
            "ssh_command": "ssh -i ~/.ssh/<YOUR_KEY>.pem ubuntu@<YOUR_EC2_IP>",
            "notes": "Primary AWS server running all production workloads"
        }
    ]

    for m in machines:
        await pool.execute(
            """
            INSERT INTO machines (name, ip, ssh_command, notes)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (name) DO NOTHING
            """,
            m["name"], m["ip"], m["ssh_command"], m["notes"]
        )

    print(f"Seeded {len(machines)} machines")


async def seed_projects(pool: asyncpg.Pool):
    """Seed projects table."""
    # Get machine IDs
    mac_studio = await pool.fetchrow("SELECT id FROM machines WHERE name = 'mac-studio'")
    slmbeast = await pool.fetchrow("SELECT id FROM machines WHERE name = 'slmbeast'")
    aws = await pool.fetchrow("SELECT id FROM machines WHERE name = 'aws-ec2'")

    projects = [
        {
            "name": "recipe.sync",
            "path": "~/dev/recipe_sync",
            "machine_id": mac_studio["id"],
            "status": "active",
            "tech_stack": {
                "frontend": "Flutter 3.x",
                "backend": "FastAPI",
                "database": "PostgreSQL",
                "state": "Riverpod",
                "ai": "Claude API"
            },
            "current_phase": "Beta development"
        },
        {
            "name": "wine.dine Pro",
            "path": "~/dev/wine_dine_pro",
            "machine_id": mac_studio["id"],
            "status": "production",
            "tech_stack": {
                "frontend": "Flutter 3.32.5",
                "backend": "FastAPI",
                "state": "Provider",
                "subscription": "in_app_purchase"
            },
            "current_phase": "Production v1.0.8"
        },
        {
            "name": "wine.dine",
            "path": "~/dev/wine_dine",
            "machine_id": mac_studio["id"],
            "status": "production",
            "tech_stack": {
                "frontend": "Flutter",
                "backend": "FastAPI (shared with Pro)"
            },
            "current_phase": "Production - free tier"
        },
        {
            "name": "friendly-robots-website",
            "path": "~/dev/fr_website",
            "machine_id": mac_studio["id"],
            "status": "production",
            "tech_stack": {
                "frontend": "Static HTML/CSS",
                "hosting": "Docker + Nginx"
            },
            "current_phase": "Deployed"
        }
    ]

    for p in projects:
        await pool.execute(
            """
            INSERT INTO projects (name, path, machine_id, status, tech_stack, current_phase)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (name) DO UPDATE SET
                path = $2,
                machine_id = $3,
                status = $4,
                tech_stack = $5,
                current_phase = $6,
                updated_at = NOW()
            """,
            p["name"], p["path"], p["machine_id"], p["status"],
            json.dumps(p["tech_stack"]), p["current_phase"]
        )

    print(f"Seeded {len(projects)} projects")


async def seed_containers(pool: asyncpg.Pool):
    """Seed containers table."""
    aws = await pool.fetchrow("SELECT id FROM machines WHERE name = 'aws-ec2'")

    containers = [
        {
            "name": "recipe_sync_api",
            "machine_id": aws["id"],
            "project": "recipe.sync",
            "ports": "8001:8000",
            "compose_path": "~/recipe_sync_api/docker-compose.yml"
        },
        {
            "name": "winedine_api",
            "machine_id": aws["id"],
            "project": "wine.dine Pro",
            "ports": "8002:8000",
            "compose_path": "~/winedine_api/docker-compose.yml"
        },
        {
            "name": "friendly-robots-web",
            "machine_id": aws["id"],
            "project": "friendly-robots-website",
            "ports": "8080:80",
            "compose_path": "~/fr_website/docker-compose.yml"
        }
    ]

    for c in containers:
        await pool.execute(
            """
            INSERT INTO containers (name, machine_id, project, ports, compose_path)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT DO NOTHING
            """,
            c["name"], c["machine_id"], c["project"], c["ports"], c["compose_path"]
        )

    print(f"Seeded {len(containers)} containers")


async def seed_key_files(pool: asyncpg.Pool):
    """Seed key_files table."""
    wine_dine = await pool.fetchrow("SELECT id FROM projects WHERE name = 'wine.dine Pro'")
    recipe_sync = await pool.fetchrow("SELECT id FROM projects WHERE name = 'recipe.sync'")

    key_files = [
        {
            "project_id": wine_dine["id"],
            "file_path": "lib/ui/subscription_gate.dart",
            "line_hint": 70,
            "description": "debugBypass flag - MUST be false in production builds!",
            "importance": "critical"
        },
        {
            "project_id": wine_dine["id"],
            "file_path": "lib/ui/subscription_screen.dart",
            "line_hint": None,
            "description": "Dynamic pricing implementation - uses ProductDetails.price",
            "importance": "important"
        },
        {
            "project_id": wine_dine["id"],
            "file_path": "pubspec.yaml",
            "line_hint": 19,
            "description": "Version number - check before building",
            "importance": "important"
        },
        {
            "project_id": recipe_sync["id"],
            "file_path": "memories/DECISIONS.md",
            "line_hint": None,
            "description": "All architectural decisions with rationale",
            "importance": "reference"
        }
    ]

    for f in key_files:
        await pool.execute(
            """
            INSERT INTO key_files (project_id, file_path, line_hint, description, importance)
            VALUES ($1, $2, $3, $4, $5)
            """,
            f["project_id"], f["file_path"], f["line_hint"], f["description"], f["importance"]
        )

    print(f"Seeded {len(key_files)} key files")


async def seed_guardrails(pool: asyncpg.Pool):
    """Seed guardrails table."""
    wine_dine = await pool.fetchrow("SELECT id FROM projects WHERE name = 'wine.dine Pro'")

    guardrails = [
        {
            "project_id": wine_dine["id"],
            "description": "Verify debugBypass = false before building",
            "check_type": "pre_build",
            "file_path": "lib/ui/subscription_gate.dart",
            "pattern": "debugBypass = false",
            "severity": "critical"
        },
        {
            "project_id": wine_dine["id"],
            "description": "Use dynamic pricing (ProductDetails.price), never hardcode prices",
            "check_type": "always",
            "file_path": None,
            "pattern": None,
            "severity": "critical"
        },
        {
            "project_id": None,  # Global
            "description": "Never force push to main/master branch",
            "check_type": "pre_push",
            "file_path": None,
            "pattern": None,
            "severity": "critical"
        },
        {
            "project_id": None,
            "description": "Run flutter clean before release builds",
            "check_type": "pre_build",
            "file_path": None,
            "pattern": None,
            "severity": "warning"
        }
    ]

    for g in guardrails:
        await pool.execute(
            """
            INSERT INTO guardrails (project_id, description, check_type, file_path, pattern, severity)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            g["project_id"], g["description"], g["check_type"],
            g["file_path"], g["pattern"], g["severity"]
        )

    print(f"Seeded {len(guardrails)} guardrails")


async def seed_lessons(pool: asyncpg.Pool, openai: AsyncOpenAI):
    """Seed lessons table with embeddings."""
    wine_dine = await pool.fetchrow("SELECT id FROM projects WHERE name = 'wine.dine Pro'")
    recipe_sync = await pool.fetchrow("SELECT id FROM projects WHERE name = 'recipe.sync'")

    lessons = [
        {
            "title": "iOS Share Sheet requires sharePositionOrigin",
            "content": "On iOS, the Share Sheet will crash on iPads if sharePositionOrigin is not provided. This is required for ALL iOS devices, not just iPads. Always include it when using share functionality.",
            "project_id": None,  # Cross-project
            "tags": ["flutter", "ios", "share-sheet", "ipad"],
            "severity": "critical"
        },
        {
            "title": "Never use debugBypass=true in production",
            "content": "The debugBypass flag in subscription_gate.dart must ALWAYS be false in production builds. Setting it to true bypasses subscription checks and results in lost revenue. This was learned the hard way.",
            "project_id": wine_dine["id"],
            "tags": ["flutter", "subscription", "production"],
            "severity": "critical"
        },
        {
            "title": "Use dynamic pricing from ProductDetails",
            "content": "Never hardcode subscription prices like '$1.99'. Always use ProductDetails.price from the in_app_purchase package. App stores require dynamic pricing and will reject apps with hardcoded prices.",
            "project_id": wine_dine["id"],
            "tags": ["flutter", "subscription", "app-store", "google-play"],
            "severity": "critical"
        },
        {
            "title": "GoRouter dialogs require navigatorKey pattern",
            "content": "When using GoRouter, dialogs don't work properly with the default navigation. Use the navigatorKey + currentContext pattern from decision D032 in recipe.sync.",
            "project_id": None,
            "tags": ["flutter", "go_router", "dialogs", "navigation"],
            "severity": "important"
        },
        {
            "title": "OpenAI content filters block wine/alcohol content",
            "content": "OpenAI's vision API may block or refuse to process images containing wine bottles or alcohol-related content due to content filters. Use Claude Opus for wine.dine Pro instead.",
            "project_id": wine_dine["id"],
            "tags": ["ai", "openai", "content-filter", "vision"],
            "severity": "important"
        },
        {
            "title": "Claude Opus 4.5 best for multi-image OCR",
            "content": "For recipe scanning with multiple images, Claude Opus 4.5 provides the highest accuracy. Haiku is fine for single-page simple recipes, but complex multi-page recipes need Opus.",
            "project_id": recipe_sync["id"],
            "tags": ["ai", "claude", "ocr", "recipes"],
            "severity": "tip"
        },
        {
            "title": "CocoaPods conflicts - delete and rebuild",
            "content": "When encountering CocoaPods conflicts in iOS builds, delete the Pods/ directory and Podfile.lock, then run pod install again. Don't try to manually resolve version conflicts.",
            "project_id": None,
            "tags": ["flutter", "ios", "cocoapods", "build"],
            "severity": "tip"
        },
        {
            "title": "Policy violations have ~7 day fix window",
            "content": "When receiving a policy violation notice from Google Play or App Store, you typically have about 7 days to fix and resubmit before the app is removed. Act quickly but don't panic.",
            "project_id": None,
            "tags": ["app-store", "google-play", "policy"],
            "severity": "important"
        }
    ]

    for lesson in lessons:
        # Check if lesson already exists
        existing = await pool.fetchrow(
            "SELECT id FROM lessons WHERE title = $1",
            lesson["title"]
        )
        if existing:
            continue

        # Generate embedding
        embed_text = f"{lesson['title']}\n{lesson['content']}"
        embedding = await get_embedding(openai, embed_text)
        embedding_str = format_embedding(embedding)

        await pool.execute(
            """
            INSERT INTO lessons (title, content, project_id, tags, severity, embedding)
            VALUES ($1, $2, $3, $4, $5, $6::vector)
            """,
            lesson["title"], lesson["content"], lesson["project_id"],
            lesson["tags"], lesson["severity"], embedding_str
        )

    print(f"Seeded {len(lessons)} lessons with embeddings")


async def seed_patterns(pool: asyncpg.Pool, openai: AsyncOpenAI):
    """Seed patterns table with embeddings."""
    patterns = [
        {
            "name": "GoRouter Dialog Pattern",
            "problem": "Dialogs don't work correctly when using GoRouter for navigation",
            "solution": "Use navigatorKey with currentContext pattern: create a GlobalKey<NavigatorState>, pass it to MaterialApp.router, then use navigatorKey.currentContext for showDialog calls",
            "code_example": """final navigatorKey = GlobalKey<NavigatorState>();

MaterialApp.router(
  routerConfig: goRouter,
  builder: (context, child) {
    return Navigator(
      key: navigatorKey,
      onGenerateRoute: (_) => MaterialPageRoute(
        builder: (_) => child!,
      ),
    );
  },
);

// Then use:
showDialog(
  context: navigatorKey.currentContext!,
  builder: ...
);""",
            "applies_to": ["flutter", "go_router", "dialogs"]
        },
        {
            "name": "AWS Deployment Pattern",
            "problem": "Need to deploy updated backend code to AWS EC2",
            "solution": "SCP files to the server, then docker-compose restart. Always check logs after restart.",
            "code_example": """# Upload files
scp -i ~/.ssh/<YOUR_KEY>.pem local/file.py ubuntu@<YOUR_EC2_IP>:~/app/

# Restart service
ssh -i ~/.ssh/<YOUR_KEY>.pem ubuntu@<YOUR_EC2_IP> "cd ~/app && docker-compose restart api"

# Check logs
ssh -i ~/.ssh/<YOUR_KEY>.pem ubuntu@<YOUR_EC2_IP> "docker logs container_name --tail 100"
""",
            "applies_to": ["aws", "docker", "deployment"]
        },
        {
            "name": "Flutter Release Build Pattern",
            "problem": "Need to build a release version of Flutter app",
            "solution": "Always clean first, check version, verify debug flags are off, then build",
            "code_example": """# Check version
grep "version:" pubspec.yaml

# Clean and rebuild
flutter clean && flutter pub get

# Android
flutter build appbundle --release

# iOS
flutter build ipa --release
""",
            "applies_to": ["flutter", "android", "ios", "release"]
        }
    ]

    for pattern in patterns:
        # Check if pattern already exists
        existing = await pool.fetchrow(
            "SELECT id FROM patterns WHERE name = $1",
            pattern["name"]
        )
        if existing:
            continue

        embed_text = f"{pattern['name']}\n{pattern['problem']}\n{pattern['solution']}"
        embedding = await get_embedding(openai, embed_text)
        embedding_str = format_embedding(embedding)

        await pool.execute(
            """
            INSERT INTO patterns (name, problem, solution, code_example, applies_to, embedding)
            VALUES ($1, $2, $3, $4, $5, $6::vector)
            """,
            pattern["name"], pattern["problem"], pattern["solution"],
            pattern["code_example"], pattern["applies_to"], embedding_str
        )

    print(f"Seeded {len(patterns)} patterns with embeddings")


async def seed_permissions(pool: asyncpg.Pool):
    """Seed permissions table."""
    permissions = [
        # Allowed without confirmation
        {"scope": "global", "action_type": "file_read", "pattern": "*", "allowed": True, "requires_confirmation": False, "notes": "Can read any file"},
        {"scope": "global", "action_type": "web_search", "pattern": "*", "allowed": True, "requires_confirmation": False, "notes": "Can search the web anytime"},
        {"scope": "global", "action_type": "bash", "pattern": "flutter *", "allowed": True, "requires_confirmation": False, "notes": "Flutter commands pre-approved"},
        {"scope": "global", "action_type": "bash", "pattern": "git add *", "allowed": True, "requires_confirmation": False, "notes": None},
        {"scope": "global", "action_type": "bash", "pattern": "git commit *", "allowed": True, "requires_confirmation": False, "notes": None},
        {"scope": "global", "action_type": "bash", "pattern": "git push", "allowed": True, "requires_confirmation": False, "notes": "Regular push allowed"},
        {"scope": "global", "action_type": "bash", "pattern": "ssh *", "allowed": True, "requires_confirmation": False, "notes": None},
        {"scope": "global", "action_type": "bash", "pattern": "scp *", "allowed": True, "requires_confirmation": False, "notes": None},

        # Requires confirmation
        {"scope": "global", "action_type": "bash", "pattern": "rm -rf *", "allowed": True, "requires_confirmation": True, "notes": "Destructive - ask first"},
        {"scope": "global", "action_type": "bash", "pattern": "git push --force*", "allowed": True, "requires_confirmation": True, "notes": "Force push - ask first"},
        {"scope": "global", "action_type": "bash", "pattern": "git reset --hard*", "allowed": True, "requires_confirmation": True, "notes": "Destructive - ask first"},
        {"scope": "global", "action_type": "deploy", "pattern": "*", "allowed": True, "requires_confirmation": True, "notes": "Always confirm before deploying to production"},
    ]

    for p in permissions:
        await pool.execute(
            """
            INSERT INTO permissions (scope, action_type, pattern, allowed, requires_confirmation, notes)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            p["scope"], p["action_type"], p["pattern"],
            p["allowed"], p["requires_confirmation"], p["notes"]
        )

    print(f"Seeded {len(permissions)} permissions")


async def seed_workflows(pool: asyncpg.Pool):
    """Seed workflows table."""
    slmbeast = await pool.fetchrow("SELECT id FROM machines WHERE name = 'slmbeast'")

    workflows = [
        {
            "name": "Local LLM for Recipe Generation",
            "description": "Use Qwen2.5:32b on slmbeast for recipe-related AI tasks to save API costs",
            "steps": [
                {"step": 1, "action": "Connect to slmbeast Ollama", "command": "curl http://<YOUR_LOCAL_SERVER_IP>:11434/api/generate"},
                {"step": 2, "action": "Use qwen2.5:32b model", "notes": "Good balance of speed and quality for recipes"},
                {"step": 3, "action": "For complex tasks, fall back to Claude API", "notes": None}
            ],
            "tools_used": ["ollama", "qwen2.5"],
            "project_id": None
        }
    ]

    for w in workflows:
        await pool.execute(
            """
            INSERT INTO workflows (name, description, steps, tools_used, project_id)
            VALUES ($1, $2, $3, $4, $5)
            """,
            w["name"], w["description"], json.dumps(w["steps"]), w["tools_used"], w["project_id"]
        )

    print(f"Seeded {len(workflows)} workflows")


async def main():
    """Main seeding function."""
    print("Connecting to database...")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    openai = AsyncOpenAI(api_key=OPENAI_API_KEY)

    try:
        print("\nSeeding data...\n")

        await seed_machines(pool)
        await seed_projects(pool)
        await seed_containers(pool)
        await seed_key_files(pool)
        await seed_guardrails(pool)
        await seed_permissions(pool)
        await seed_workflows(pool)

        # These require OpenAI for embeddings
        if OPENAI_API_KEY:
            await seed_lessons(pool, openai)
            await seed_patterns(pool, openai)
        else:
            print("WARNING: OPENAI_API_KEY not set, skipping lessons and patterns (need embeddings)")

        print("\n=== Seeding Complete ===")

    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
