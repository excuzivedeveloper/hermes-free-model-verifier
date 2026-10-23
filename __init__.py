"""Hermes plugin: manual free-model checks, no default routing changes."""
from __future__ import annotations


def _setup(parser):
    subs = parser.add_subparsers(dest="freeverify_command", required=True)
    view = subs.add_parser("list", help="Show last saved verification results (offline)")
    view.add_argument("--json", action="store_true", help="Machine-readable results")
    scan = subs.add_parser("scan", help="Run an explicitly requested model probe")
    scan.add_argument("--provider", choices=("openrouter", "nvidia", "gemini"), required=True)
    scan.add_argument("--model", action="append", default=[], help="Exact provider model ID (repeatable)")
    scan.add_argument("--mode", choices=("text", "tools"), default="tools")
    scan.add_argument("--limit", type=int, default=5,
                      help="Max number of discovered candidates to probe; 0 means all (default: 5)")
    scan.add_argument("--ack-unpriced", action="store_true",
                      help="Acknowledge that NVIDIA/Gemini metadata cannot establish Free Tier pricing")


def _dispatch(args):
    from .verifier import run_list, run_scan
    if args.freeverify_command == "list":
        run_list(as_json=args.json)
    elif args.freeverify_command == "scan":
        run_scan(args.provider, args.model, args.mode, args.limit, args.ack_unpriced)


def _slash(raw_args: str) -> str:
    from .verifier import chat_status
    return chat_status(raw_args)


def register(ctx):
    ctx.register_cli_command(
        name="freeverify",
        help="Manually probe free-model chat and tool-calling availability",
        description="No scheduled API calls or automatic model switching",
        setup_fn=_setup,
        handler_fn=_dispatch,
    )
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            name="freeverify",
            handler=_slash,
            description="Show the saved manual free-model verification status",
            args_hint="",
        )
