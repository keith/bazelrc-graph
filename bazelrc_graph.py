#!/usr/bin/env python3
"""Parse bazelrc files and render a graphviz graph of the config structure.

The graph shows bazel configs (the `foo` in `build:foo --flag`) and the edges
between them created by `--config=foo` references.

It also shows the file import graph: `import` edges are solid, `try-import`
edges are dotted. Files that don't exist are drawn dashed.

Configs that are referenced but never defined are drawn with a dashed
red style, and a warning is printed.

Platform-specific configs (the names bazel recognizes for
--enable_platform_specific_config: linux, macos, windows, freebsd, openbsd) are
drawn as boxes.
"""

import argparse
import os
import shlex
import subprocess
import sys

_PLATFORM_CONFIGS = {"linux", "macos", "windows", "freebsd", "openbsd"}

# Sentinel for flags that appear outside any config (e.g. `build --config=x`).
_DEFAULT = "<default>"


class Graph:
    def __init__(self, workspace):
        self.workspace = workspace
        # Configs that are defined somewhere (appear as `command:config`).
        self.defined_configs = set()
        # (source config or _DEFAULT, referenced config) pairs.
        self.config_edges = set()
        # referenced config -> list of "file:line" locations, for warnings.
        self.references = {}
        # Files that were parsed or imported, canonical path -> exists.
        self.files = {}
        # (importer path, imported path, optional) tuples.
        self.import_edges = set()
        self.warnings = []

    def warn(self, message):
        self.warnings.append(message)
        print("warning: {}".format(message), file=sys.stderr)

    def rel(self, path):
        try:
            relative = os.path.relpath(path, self.workspace)
        except ValueError:
            return path
        if relative.startswith(".."):
            return path
        return relative


def _tokenize(line):
    try:
        return shlex.split(line, comments=False, posix=True)
    except ValueError:
        return line.split()


def _logical_lines(text):
    """Yield (line_number, line) with backslash continuations joined."""
    pending = ""
    start = None
    for number, raw in enumerate(text.splitlines(), start=1):
        if start is None:
            start = number
        line = pending + raw.lstrip() if pending else raw
        if line.rstrip().endswith("\\"):
            pending = line.rstrip()[:-1] + " "
            continue
        yield start, line
        pending = ""
        start = None
    if pending.strip():
        yield start, pending


def _config_references(tokens):
    """Yield config names referenced via --config=NAME or --config NAME."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--config="):
            name = token[len("--config=") :]
            if name:
                yield name
        elif token == "--config" and index + 1 < len(tokens):
            index += 1
            yield tokens[index]
        index += 1


def _resolve_import(path, importer, workspace):
    if path.startswith("%workspace%"):
        path = workspace + path[len("%workspace%") :]
    elif not os.path.isabs(path):
        path = os.path.join(os.path.dirname(importer), path)
    return os.path.normpath(path)


def parse_file(path, graph, visited=None):
    """Parse one bazelrc file (and its imports) into the graph."""
    if visited is None:
        visited = set()
    canonical = os.path.normpath(os.path.abspath(path))
    if canonical in visited:
        return
    visited.add(canonical)

    exists = os.path.isfile(canonical)
    graph.files[canonical] = exists
    if not exists:
        return

    with open(canonical, encoding="utf-8") as handle:
        text = handle.read()

    for number, line in _logical_lines(text):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = _tokenize(line)
        if not tokens:
            continue
        first = tokens[0]

        if first in ("import", "try-import"):
            if len(tokens) < 2:
                graph.warn(
                    "{}:{}: {} with no file".format(graph.rel(canonical), number, first)
                )
                continue
            optional = first == "try-import"
            imported = _resolve_import(tokens[1], canonical, graph.workspace)
            graph.import_edges.add((canonical, imported, optional))
            if not os.path.isfile(imported):
                graph.files.setdefault(imported, False)
                if not optional:
                    graph.warn(
                        "{}:{}: imported file '{}' does not exist".format(
                            graph.rel(canonical), number, graph.rel(imported)
                        )
                    )
                continue
            parse_file(imported, graph, visited)
            continue

        # `command:config --flags...`; the command itself (build, test,
        # common, ...) is deliberately ignored, only the config matters.
        source = _DEFAULT
        if ":" in first:
            _, config = first.split(":", 1)
            if config:
                graph.defined_configs.add(config)
                source = config
        for referenced in _config_references(tokens[1:]):
            graph.config_edges.add((source, referenced))
            graph.references.setdefault(referenced, []).append(
                "{}:{}".format(graph.rel(canonical), number)
            )


def _quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_dot(graph):
    referenced = {target for _, target in graph.config_edges}
    missing = sorted(referenced - graph.defined_configs)
    for name in missing:
        locations = ", ".join(graph.references.get(name, []))
        graph.warn(
            "config '{}' is referenced but never defined ({})".format(name, locations)
        )

    lines = []
    out = lines.append
    out("digraph bazelrc {")
    out("  rankdir=LR;")
    out('  fontname="Helvetica";')
    out('  node [fontname="Helvetica"];')
    out('  edge [fontname="Helvetica"];')
    out("")
    out("  // Config graph")

    def config_id(name):
        return _quote("config: " + name)

    all_configs = sorted(graph.defined_configs | referenced)
    has_default = any(source == _DEFAULT for source, _ in graph.config_edges)
    if has_default:
        out(
            '  {} [label="default", shape=box];'.format(
                config_id(_DEFAULT)
            )
        )
    for name in all_configs:
        shape = "box" if name in _PLATFORM_CONFIGS else "ellipse"
        attrs = ["label={}".format(_quote(name)), "shape={}".format(shape)]
        if name not in graph.defined_configs:
            attrs += ["style=dashed", 'color="#cc0000"', 'fontcolor="#cc0000"']
        out("  {} [{}];".format(config_id(name), ", ".join(attrs)))
    for source, target in sorted(
        graph.config_edges, key=lambda e: (e[0] != _DEFAULT, e)
    ):
        out("  {} -> {};".format(config_id(source), config_id(target)))

    out("")
    out("  // File import graph")
    out("  subgraph cluster_imports {")
    out('    label="file imports";')
    out("    fontsize=10;")
    out('    style="rounded,dashed";')
    out('    color="#999999";')
    out("    node [shape=note, fontsize=10];")

    def file_id(path):
        return _quote("file: " + graph.rel(path))

    for path in sorted(graph.files, key=graph.rel):
        attrs = ["label={}".format(_quote(graph.rel(path)))]
        if not graph.files[path]:
            attrs += ["style=dashed", 'color="#999999"', 'fontcolor="#999999"']
        out("    {} [{}];".format(file_id(path), ", ".join(attrs)))
    for importer, imported, optional in sorted(
        graph.import_edges, key=lambda e: (graph.rel(e[0]), graph.rel(e[1]))
    ):
        style = " [style=dotted]" if optional else ""
        out("    {} -> {}{};".format(file_id(importer), file_id(imported), style))
    out("  }")
    out("}")
    return "\n".join(lines) + "\n"


def open_file(path):
    if sys.platform == "darwin":
        opener = "open"
    elif sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
        return 0
    else:
        opener = "xdg-open"
    return subprocess.run(
        [opener, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


def _main():
    parser = argparse.ArgumentParser(
        description="Graph the config structure of bazelrc files."
    )
    parser.add_argument(
        "bazelrc",
        help="path to the root bazelrc file",
        default=os.path.join(os.getcwd(), ".bazelrc"),
    )
    parser.add_argument(
        "--workspace",
        help="workspace root used to resolve %%workspace%% in imports "
        "(default: the directory containing the root bazelrc)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=".",
        help="directory the .dot and .png are written to (default: cwd)",
    )
    parser.add_argument(
        "--name",
        default="bazelrc-graph",
        help="base name of the output files (default: bazelrc-graph)",
    )
    parser.add_argument(
        "--no-png",
        action="store_true",
        help="only write the .dot file, skip rendering the png",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="open the rendered png when done",
    )
    args = parser.parse_args()

    root = os.path.abspath(args.bazelrc)
    if not os.path.isfile(root):
        parser.error("no such file: {}".format(args.bazelrc))
    workspace = os.path.abspath(args.workspace or os.path.dirname(root))

    graph = Graph(workspace)
    parse_file(root, graph)
    dot = render_dot(graph)

    os.makedirs(args.output_dir, exist_ok=True)
    dot_path = os.path.join(args.output_dir, args.name + ".dot")
    with open(dot_path, "w", encoding="utf-8") as handle:
        handle.write(dot)
    print(dot_path)

    if args.no_png:
        return 0

    png_path = os.path.join(args.output_dir, args.name + ".png")
    try:
        subprocess.run(["dot", "-Tpng", "-o", png_path, dot_path], check=True)
    except FileNotFoundError:
        print(
            "warning: graphviz 'dot' not found, skipping png rendering",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as error:
        print("error: dot failed: {}".format(error), file=sys.stderr)
        return 1
    print(png_path)

    if args.open:
        return open_file(png_path)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
