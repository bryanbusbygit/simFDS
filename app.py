import os
import re
import tempfile
import subprocess
from pathlib import Path

from flask import Flask, render_template, request, session, redirect, url_for

# ---------------------------------------------------------
# Paths and basic setup
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
BIN_DIR = BASE_DIR / "bin"
SIMFDS_PATH = BIN_DIR / "simFDS"

app = Flask(__name__)
# For session storage of last system text (local dev, replace in production)
app.secret_key = "jaguar-simfds-secret-key"

DEFAULT_SYSTEM_TEXT = (
    "NUMBER OF VARIABLES: 3\n"
    "NUMBER OF STATES: 2\n"
    "x1 = x2\n"
    "x2 = x1 + x3\n"
    "x3 = x2 + x1\n"
)


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
def run_command(cmd, cwd):
    """Run a command, return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _subscript_label(var_name: str) -> str:
    """
    Convert 'x1' -> 'x₁', 'x12' -> 'x₁₂' using Unicode subscripts.
    If the name does not match x + digits, return it unchanged.
    """
    m = re.fullmatch(r"x(\d+)", var_name)
    if not m:
        return var_name

    digits = m.group(1)
    subs = {
        "0": "₀",
        "1": "₁",
        "2": "₂",
        "3": "₃",
        "4": "₄",
        "5": "₅",
        "6": "₆",
        "7": "₇",
        "8": "₈",
        "9": "₉",
    }
    return "x" + "".join(subs.get(d, d) for d in digits)


def build_dependency_dot(system_text: str) -> str:
    """
    Build dependency digraph DOT.

    Convention (your choice):
      Equation: xk = f(x_i1, ..., x_im)
      Edges:    xk -> x_i1, ..., xk -> x_im.
    """
    edges = set()
    nodes = set()
    assign_re = re.compile(r"^\s*(x\d+)\s*=")
    var_re = re.compile(r"\b(x\d+)\b")

    for line in system_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = assign_re.match(line)
        if not m:
            continue

        target = m.group(1)  # xk on the left
        nodes.add(target)

        rhs = line.split("=", 1)[1]
        for var in var_re.findall(rhs):
            nodes.add(var)
            if var != target:
                # NOTE: flipped direction: x_k -> x_i
                edges.add((target, var))

    if not edges and not nodes:
        return ""

    out_lines = [
        "digraph dep {",
        "  rankdir=LR;",
        '  node [shape=ellipse, fontname="Monaco", fontsize=11];',
        '  edge [fontname="Monaco", fontsize=9];',
    ]

    # Node declarations with math-style labels x₁, x₂, ...
    for v in sorted(nodes):
        label = _subscript_label(v)
        out_lines.append(f'  "{v}" [label="{label}"];')

    # Edges x_k -> x_i
    for u, v in sorted(edges):
        out_lines.append(f'  "{u}" -> "{v}";')

    out_lines.append("}")
    return "\n".join(out_lines)


def _reorder_statespace_dot(dot_source: str) -> str:
    """
    Reorder the state-space DOT so that edge lines are grouped by source
    state ordered by (Hamming weight, integer value of the bitstring).

    Lines of interest look like:
        "0 0 0 0" -> "0 0 0 1";
    """
    lines = dot_source.splitlines()
    edge_pattern = re.compile(r'"([01 ]+)"\s*->\s*"[01 ]+"')

    edge_lines = []
    prefix_lines = []
    closing_line = ""

    for line in lines:
        stripped = line.strip()
        if stripped == "}":
            closing_line = line
            continue

        if edge_pattern.search(line):
            edge_lines.append(line)
        else:
            prefix_lines.append(line)

    def key_for_line(line: str):
        m = edge_pattern.search(line)
        if not m:
            return (0, 0)
        raw = m.group(1)
        bits = raw.replace(" ", "")
        weight = bits.count("1")
        value = int(bits, 2) if bits else 0
        return (weight, value)

    edge_lines_sorted = sorted(edge_lines, key=key_for_line)

    new_lines = prefix_lines + edge_lines_sorted
    if closing_line:
        new_lines.append(closing_line)

    return "\n".join(new_lines)


def compute_system_artifacts(system_text: str):
    """
    Given system_text, run simFDS and Graphviz once and return:
      statespace_svg (str),
      depgraph_svg (str),
      limitcycles_text (str),
      statespace_dot_text (str, reordered)
    All SVGs are returned as inline text, not saved to disk.
    """
    statespace_svg = ""
    depgraph_svg = ""
    limitcycles_text = ""
    statespace_dot_text = ""

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        pds_path = tmpdir / "system.pds"
        pds_path.write_text(system_text, encoding="utf-8")

        # Run simFDS if binary exists
        if SIMFDS_PATH.exists():
            run_command([str(SIMFDS_PATH), "system"], cwd=tmpdir)
        # If it doesn't exist, UI will just show placeholders.

        # State space DOT -> SVG
        ss_dot = tmpdir / "system-statespace.dot"
        ss_svg = tmpdir / "system-statespace.svg"
        if ss_dot.exists():
            raw_dot = ss_dot.read_text(encoding="utf-8")
            statespace_dot_text = _reorder_statespace_dot(raw_dot)

            rc, _, _ = run_command(
                ["dot", "-Tsvg", "-o", str(ss_svg), str(ss_dot)],
                cwd=tmpdir,
            )
            if rc == 0 and ss_svg.exists():
                statespace_svg = ss_svg.read_text(encoding="utf-8")

        # Limit cycles text
        lc_txt = tmpdir / "system-limitcycles.txt"
        if lc_txt.exists():
            limitcycles_text = lc_txt.read_text(encoding="utf-8")

        # Dependency graph from system_text
        dep_dot_src = build_dependency_dot(system_text)
        if dep_dot_src:
            dep_dot = tmpdir / "system-dep.dot"
            dep_svg = tmpdir / "system-dep.svg"
            dep_dot.write_text(dep_dot_src, encoding="utf-8")
            rc, _, _ = run_command(
                ["dot", "-Tsvg", "-o", str(dep_svg), str(dep_dot)],
                cwd=tmpdir,
            )
            if rc == 0 and dep_svg.exists():
                depgraph_svg = dep_svg.read_text(encoding="utf-8")

    return statespace_svg, depgraph_svg, limitcycles_text, statespace_dot_text


# ---------------------------------------------------------
# Routes
# ---------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    system_text = session.get("system_text", DEFAULT_SYSTEM_TEXT)
    statespace_svg = ""
    depgraph_svg = ""
    limitcycles_text = ""
    statespace_dot_text = ""

    if request.method == "POST":
        system_text = request.form.get("system_text", "").strip() or DEFAULT_SYSTEM_TEXT
        session["system_text"] = system_text

        (
            statespace_svg,
            depgraph_svg,
            limitcycles_text,
            statespace_dot_text,
        ) = compute_system_artifacts(system_text)

    return render_template(
        "index.html",
        system_text=system_text,
        statespace_svg=statespace_svg,
        depgraph_svg=depgraph_svg,
        limitcycles_text=limitcycles_text,
        statespace_dot=statespace_dot_text,
    )


@app.route("/graph/<kind>")
def graph_view(kind):
    """
    Fullscreen view for a single graph on an infinite white canvas.
    Recomputes from the last system_text stored in the session.
    """
    system_text = session.get("system_text", DEFAULT_SYSTEM_TEXT)

    if kind not in {"statespace", "dependency"}:
        return redirect(url_for("index"))

    statespace_svg, depgraph_svg, _, _ = compute_system_artifacts(system_text)

    if kind == "statespace":
        svg = statespace_svg
        title = "State space – Jaguar"
    else:
        svg = depgraph_svg
        title = "Dependency graph – Jaguar"

    if not svg:
        return redirect(url_for("index"))

    return render_template("graph_view.html", title=title, svg=svg)


if __name__ == "__main__":
    app.run(debug=True)
