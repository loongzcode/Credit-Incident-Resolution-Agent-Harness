"""Trusted local operator CLI. No Agent, funding, or repair commands."""

import argparse
import json
from pathlib import Path

from credit_harness.domain.enums import ScenarioId, ToolName
from credit_harness.persistence.store import SimulatorAdmin, create_schema, open_engine
from credit_harness.settings import database_url
from credit_harness.simulator.scenarios import ORDER_ID, build_scenario


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create simulator tables without resetting existing data")
    seed = commands.add_parser("seed", help="create an isolated world and a read-only tool grant")
    seed.add_argument("scenario", choices=[s.value for s in ScenarioId])
    seed.add_argument("--credentials-file", type=Path, required=True)
    seed.add_argument("--allow-raw", action="store_true", help="explicitly allow raw Callback reads")
    advance = commands.add_parser("advance", help="advance virtual time, not financial state")
    advance.add_argument("simulation_id")
    advance.add_argument("seconds", type=int)
    args = parser.parse_args(argv)
    engine = open_engine(args.database_url or database_url())
    try:
        if args.command == "init":
            create_schema(engine)
            print("Simulator schema initialized; existing worlds preserved.")
        elif args.command == "seed":
            if args.credentials_file.exists():
                parser.error("credentials file already exists; choose a new path")
            admin = SimulatorAdmin(engine)
            simulation_id = admin.seed(build_scenario(ScenarioId(args.scenario)))
            tools = set(ToolName)
            if not args.allow_raw:
                tools.remove(ToolName.CALLBACK_RAW)
            token = admin.grant(simulation_id, tools)
            args.credentials_file.parent.mkdir(parents=True, exist_ok=True)
            with args.credentials_file.open("x", encoding="utf-8") as output:
                json.dump({"simulation_id": simulation_id, "order_id": ORDER_ID,
                           "tool_token": token}, output, indent=2)
            print(f"Seeded {args.scenario}; credentials saved to {args.credentials_file}")
            print("Only give the tool token and order_id to the HTTP tool caller.")
        else:
            result = SimulatorAdmin(engine).advance(args.simulation_id, args.seconds)
            print(result.isoformat())
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

