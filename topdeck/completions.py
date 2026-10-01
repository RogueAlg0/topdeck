"""Shell completions for topdeck, generated from the live parser.

`topdeck completions {bash,zsh,fish}` prints a completion script built
at runtime by introspecting build_parser(): command names, per-command
flags, watch/portfolio sub-actions, game slugs from the adapter
REGISTRY, and choice positionals like the shell name itself. Because it
is generated from the real parser, it can never drift from the CLI it
completes. The command is hidden from --help on purpose: completions
are setup, not a feature anyone browses for.
"""

from __future__ import annotations

import argparse

SHELLS = ("bash", "zsh", "fish")


def _specs():
    """Describe every command path for completion.

    Returns (specs, games): specs maps a path tuple like ("watch",
    "add") to its flags, value flags, sub-actions, whether the first
    positional is a game slug, and any choices on the first positional.
    () is the top level. The topdeck.cli import is lazy: cli imports
    this module for its command handler, so a top-level import would be
    circular.
    """
    from topdeck import adapters as game_adapters
    from topdeck.cli import build_parser

    parser = build_parser()
    games = sorted(game_adapters.REGISTRY)
    specs: dict = {}

    def describe(subparser):
        flags: list[str] = []
        value_flags: list[str] = []
        subactions: list[str] = []
        first_positional: str | None = None
        pos_choices: list[str] | None = None
        for action in subparser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subactions.extend(sorted(action.choices))
            elif action.option_strings:
                flags.extend(action.option_strings)
                if action.nargs != 0:
                    value_flags.extend(action.option_strings)
            elif first_positional is None:
                first_positional = action.dest
                if isinstance(action.choices, (list, tuple)):
                    pos_choices = list(action.choices)
        return {
            "flags": sorted(set(flags)),
            "value_flags": sorted(set(value_flags)),
            "subactions": subactions,
            "game_first": first_positional == "game",
            "pos_choices": pos_choices,
        }

    def visit(path, subparser):
        specs[path] = describe(subparser)
        for action in subparser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name in sorted(action.choices):
                    visit(path + (name,), action.choices[name])

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name in sorted(action.choices):
                visit((name,), action.choices[name])
    specs[()] = describe(parser)
    return specs, games


def generate(shell: str) -> str:
    """The completion script for one shell, generated from the live parser."""
    specs, games = _specs()
    if shell == "bash":
        return _bash(specs, games)
    if shell == "zsh":
        return _zsh(specs, games)
    if shell == "fish":
        return _fish(specs, games)
    raise ValueError(f"unknown shell: {shell}")


def _word_scan(specs: dict) -> tuple[str, str, str]:
    """Shared COMP_WORDS walk for bash and zsh, as generated script text.

    Returns (skip_case, command_case, sub_case): the case patterns for
    flags that take a value, for top-level command names, and for
    watch/portfolio sub-actions.
    """
    value_flags = sorted({f for spec in specs.values() for f in spec["value_flags"]})
    patterns = list(value_flags) + [f"{flag}=*" for flag in value_flags]
    skip_case = "|".join(patterns)
    command_case = "|".join(specs[()]["subactions"])
    subs = sorted(
        {sub for path, spec in specs.items() if len(path) == 1 for sub in spec["subactions"]}
    )
    sub_case = "|".join(subs)
    return skip_case, command_case, sub_case


def _scan_function(
    specs: dict, cur_expr: str, word_expr: str, start: int, bound: str, bash: bool
) -> str:
    """The word-walk preamble shared by the bash and zsh scripts.

    Walks the words typed so far to find the command path (e.g.
    watch/add), skipping flags and their values, and notes whether a
    positional was already given. `bash` controls the COMPREPLY reset,
    which only bash uses.
    """
    skip_case, command_case, sub_case = _word_scan(specs)
    reply_reset = "    COMPREPLY=()\n" if bash else ""
    return f"""    local cur cmd sub w i skip_next positional_seen path
{reply_reset}    cur="{cur_expr}"
    cmd=""
    sub=""
    skip_next=0
    positional_seen=0
    for (( i = {start}; i < {bound}; i++ )); do
        w="{word_expr}"
        if (( skip_next )); then
            skip_next=0
            continue
        fi
        case "$w" in
            -*)
                case "$w" in
                    {skip_case}) skip_next=1 ;;
                esac
                ;;
            *)
                if [[ -z "$cmd" ]]; then
                    case "$w" in
                        {command_case}) cmd="$w" ;;
                    esac
                elif [[ "$cmd" == "watch" || "$cmd" == "portfolio" ]] && [[ -z "$sub" ]]; then
                    case "$w" in
                        {sub_case}) sub="$w" ;;
                        *) positional_seen=1 ;;
                    esac
                else
                    positional_seen=1
                fi
                ;;
        esac
    done
    path="$cmd"
    [[ -n "$sub" ]] && path="$cmd/$sub"
"""


def _bash(specs: dict, games: list[str]) -> str:
    arms = []
    for path in sorted(specs):
        spec = specs[path]
        arms.append(_bash_arm(path, spec, games))
    body = "\n".join(arms)
    scan = _scan_function(
        specs, "${COMP_WORDS[COMP_CWORD]}", "${COMP_WORDS[i]}", 1, "COMP_CWORD", True
    )
    return f"""# topdeck bash completion. Generated by `topdeck completions bash`: do not edit.

_topdeck() {{
{scan}    case "$path" in
{body}
    esac
    return 0
}}

complete -F _topdeck topdeck
"""


def _bash_arm(path: tuple, spec: dict, games: list[str]) -> str:
    """One case arm completing flags, and games or choices where they apply."""
    name = "/".join(path) if path else ""
    flags = " ".join(spec["flags"])
    if not path:
        words = " ".join(spec["subactions"])
        return (
            f'        "")\n'
            f'            if [[ "$cur" == -* ]]; then\n'
            f'                COMPREPLY=( $(compgen -W "{flags}" -- "$cur") )\n'
            f"            else\n"
            f'                COMPREPLY=( $(compgen -W "{words}" -- "$cur") )\n'
            f"            fi ;;"
        )
    if spec["subactions"]:
        words = " ".join(spec["subactions"])
    elif spec["game_first"]:
        words = " ".join(games)
    elif spec["pos_choices"]:
        words = " ".join(spec["pos_choices"])
    else:
        words = ""
    if words:
        return (
            f"        {name})\n"
            f'            if [[ "$cur" == -* ]]; then\n'
            f'                COMPREPLY=( $(compgen -W "{flags}" -- "$cur") )\n'
            f"            elif (( ! positional_seen )); then\n"
            f'                COMPREPLY=( $(compgen -W "{words}" -- "$cur") )\n'
            f"            fi ;;"
        )
    return (
        f"        {name})\n"
        f'            if [[ "$cur" == -* ]]; then\n'
        f'                COMPREPLY=( $(compgen -W "{flags}" -- "$cur") )\n'
        f"            fi ;;"
    )


def _zsh(specs: dict, games: list[str]) -> str:
    arms = []
    for path in sorted(specs):
        spec = specs[path]
        arms.append(_zsh_arm(path, spec, games))
    body = "\n".join(arms)
    scan = _scan_function(specs, "${words[CURRENT]}", "${words[i]}", 2, "CURRENT", False)
    return f"""#compdef topdeck
# topdeck zsh completion. Generated by `topdeck completions zsh`: do not edit.

_topdeck() {{
{scan}    case "$path" in
{body}
    esac
    return 0
}}
"""


def _zsh_arm(path: tuple, spec: dict, games: list[str]) -> str:
    """One case arm completing flags, and games or choices where they apply."""
    name = "/".join(path) if path else ""
    flags = " ".join(spec["flags"])
    if not path:
        words = " ".join(spec["subactions"])
        return (
            f'        "")\n'
            f'            if [[ "$cur" == -* ]]; then\n'
            f"                compadd -- {flags}\n"
            f"            else\n"
            f"                compadd -- {words}\n"
            f"            fi ;;"
        )
    if spec["subactions"]:
        words = " ".join(spec["subactions"])
    elif spec["game_first"]:
        words = " ".join(games)
    elif spec["pos_choices"]:
        words = " ".join(spec["pos_choices"])
    else:
        words = ""
    if words:
        return (
            f"        {name})\n"
            f'            if [[ "$cur" == -* ]]; then\n'
            f"                compadd -- {flags}\n"
            f"            elif (( ! positional_seen )); then\n"
            f"                compadd -- {words}\n"
            f"            fi ;;"
        )
    return (
        f"        {name})\n"
        f'            if [[ "$cur" == -* ]]; then\n'
        f"                compadd -- {flags}\n"
        f"            fi ;;"
    )


def _fish_flag(flag: str, takes_value: bool) -> str:
    """One fish flag fragment: -s h, -l json, or -l file -r for valued flags."""
    req = " -r" if takes_value else ""
    if len(flag) == 2:
        return f"-s {flag[1]}{req}"
    return f"-l {flag[2:]}{req}"


def _fish(specs: dict, games: list[str]) -> str:
    lines = [
        "# topdeck fish completion. Generated by `topdeck completions fish`: do not edit.",
        "",
    ]
    game_words = " ".join(games)

    def cond(path: tuple) -> str:
        if not path:
            return "__fish_use_subcommand"
        return "; and ".join(f"__fish_seen_subcommand_from {part}" for part in path)

    for path in sorted(specs):
        spec = specs[path]
        condition = cond(path)
        if not path:
            words = " ".join(spec["subactions"])
            lines.append(f"complete -c topdeck -f -n '{condition}' -a '{words}'")
        elif spec["subactions"]:
            words = " ".join(spec["subactions"])
            lines.append(
                f"complete -c topdeck -f"
                f" -n '{condition}; and not __fish_seen_subcommand_from {words}'"
                f" -a '{words}'"
            )
        for flag in spec["flags"]:
            lines.append(
                f"complete -c topdeck -n '{condition}'"
                f" {_fish_flag(flag, flag in spec['value_flags'])}"
            )
        if spec["game_first"]:
            lines.append(
                f"complete -c topdeck -f"
                f" -n '{condition}; and not __fish_seen_subcommand_from {game_words}'"
                f" -a '{game_words}'"
            )
        elif spec["pos_choices"]:
            words = " ".join(spec["pos_choices"])
            lines.append(
                f"complete -c topdeck -f"
                f" -n '{condition}; and not __fish_seen_subcommand_from {words}'"
                f" -a '{words}'"
            )
    lines.append("")
    return "\n".join(lines)
