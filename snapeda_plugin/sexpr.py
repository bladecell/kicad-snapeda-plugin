"""Small preserving parser for KiCad library-table s-expressions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Node:
    value: str | None
    children: tuple[Node, ...]
    start: int
    end: int


class Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.offset = 0

    def whitespace(self) -> None:
        while self.offset < len(self.text):
            if self.text[self.offset].isspace():
                self.offset += 1
            elif self.text[self.offset] == ";":
                end = self.text.find("\n", self.offset)
                self.offset = len(self.text) if end < 0 else end + 1
            else:
                return

    def parse(self) -> Node:
        self.whitespace()
        if self.offset >= len(self.text) or self.text[self.offset] != "(":
            raise ValueError("library table must begin with an s-expression")
        root = self.node()
        self.whitespace()
        if self.offset != len(self.text):
            raise ValueError("unexpected data after library table")
        return root

    def node(self) -> Node:
        self.whitespace()
        start = self.offset
        if self.offset >= len(self.text):
            raise ValueError("unexpected end of library table")
        if self.text[self.offset] == "(":
            self.offset += 1
            items: list[Node] = []
            while True:
                self.whitespace()
                if self.offset >= len(self.text):
                    raise ValueError("unclosed library table expression")
                if self.text[self.offset] == ")":
                    self.offset += 1
                    return Node(None, tuple(items), start, self.offset)
                items.append(self.node())
        if self.text[self.offset] == ")":
            raise ValueError("unexpected close parenthesis")
        if self.text[self.offset] == '"':
            self.offset += 1
            value: list[str] = []
            while self.offset < len(self.text):
                char = self.text[self.offset]
                self.offset += 1
                if char == '"':
                    return Node("".join(value), (), start, self.offset)
                if char == "\\":
                    if self.offset >= len(self.text):
                        break
                    value.append(self.text[self.offset])
                    self.offset += 1
                else:
                    value.append(char)
            raise ValueError("unclosed quoted string in library table")
        while (
            self.offset < len(self.text)
            and not self.text[self.offset].isspace()
            and self.text[self.offset] not in "()"
        ):
            self.offset += 1
        return Node(self.text[start : self.offset], (), start, self.offset)


def parse(text: str) -> Node:
    """Parse one s-expression, retaining source offsets for safe append."""
    return Parser(text).parse()


def field_value(node: Node, name: str) -> str | None:
    for child in node.children:
        if child.value is None and child.children and child.children[0].value == name:
            return child.children[1].value if len(child.children) > 1 else None
    return None


def library_names(text: str) -> set[str]:
    root = parse(text)
    return {
        name
        for child in root.children
        if child.value is None and child.children and child.children[0].value == "lib"
        if (name := field_value(child, "name")) is not None
    }


def quote(value: str) -> str:
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    )


def append_library(text: str, library: str) -> str:
    root = parse(text)
    if root.value is not None:
        raise ValueError("library table root is invalid")
    if root.end == 0:
        raise ValueError("library table root is invalid")
    return text[: root.end - 1] + "\n  " + library + "\n" + text[root.end - 1 :]


def merge_symbol_libraries(existing: str, incoming: str) -> str:
    """Append incoming top-level symbols while preserving the existing header."""
    old_root = parse(existing)
    new_root = parse(incoming)
    if (
        not old_root.children
        or old_root.children[0].value != "kicad_symbol_lib"
        or not new_root.children
        or new_root.children[0].value != "kicad_symbol_lib"
    ):
        raise ValueError("Invalid KiCad symbol library")
    old_symbols = {
        node.children[1].value.casefold()
        for node in old_root.children
        if node.value is None
        and len(node.children) > 1
        and node.children[0].value == "symbol"
    }
    added: list[str] = []
    for node in new_root.children:
        if (
            node.value is not None
            or not node.children
            or node.children[0].value != "symbol"
        ):
            continue
        if len(node.children) < 2 or node.children[1].value is None:
            raise ValueError("KiCad symbol has no name")
        name = node.children[1].value
        if name.casefold() in old_symbols:
            raise ValueError(f"Symbol already exists in SnapEDA library: {name}")
        old_symbols.add(name.casefold())
        added.append(incoming[node.start : node.end])
    if not added:
        raise ValueError("Symbol library contains no symbols")
    merged = (
        existing[: old_root.end - 1]
        + "\n  "
        + "\n  ".join(added)
        + "\n"
        + existing[old_root.end - 1 :]
    )
    return merged


def validate_symbol_inheritance(text: str) -> None:
    """Reject missing same-library bases and inheritance cycles."""
    root = parse(text)
    definitions: dict[str, str | None] = {}
    for node in root.children:
        if (
            node.value is None
            and len(node.children) > 1
            and node.children[0].value == "symbol"
            and node.children[1].value is not None
        ):
            name = node.children[1].value
            parent = next(
                (
                    child.children[1].value
                    for child in node.children
                    if child.value is None
                    and len(child.children) > 1
                    and child.children[0].value == "extends"
                ),
                None,
            )
            definitions[name.casefold()] = parent

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError(f"Cyclic KiCad symbol inheritance: {name}")
        if name in visited:
            return
        visiting.add(name)
        parent = definitions[name]
        if parent:
            prefix, separator, base = parent.partition(":")
            if not separator or prefix.casefold() == "snapeda":
                key = (base if separator else parent).casefold()
                if key not in definitions:
                    raise ValueError(f"Missing KiCad symbol base: {parent}")
                visit(key)
        visiting.remove(name)
        visited.add(name)

    for name in definitions:
        visit(name)


def rewrite_footprint_properties(text: str, footprint_names: dict[str, str]) -> str:
    """Rewrite matching Footprint properties within imported symbol definitions."""
    root = parse(text)
    replacements: list[tuple[int, int, str]] = []

    def visit(node: Node) -> None:
        if node.value is not None:
            return
        if (
            len(node.children) >= 3
            and node.children[0].value == "property"
            and node.children[1].value == "Footprint"
        ):
            value = node.children[2]
            if value.value:
                name = value.value.split(":", 1)[-1]
                replacement = footprint_names.get(name)
                if replacement is not None:
                    replacements.append(
                        (value.start, value.end, quote(f"SnapEDA:{replacement}"))
                    )
        for child in node.children:
            visit(child)

    for child in root.children:
        if (
            child.value is None
            and child.children
            and child.children[0].value == "symbol"
        ):
            visit(child)
    for start, end, value in reversed(replacements):
        text = text[:start] + value + text[end:]
    return text
