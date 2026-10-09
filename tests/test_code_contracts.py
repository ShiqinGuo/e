import ast
import io
import re
import tokenize
from pathlib import Path


def test_python_source_contract_rules():
    root = Path(__file__).resolve().parents[1] / "src"
    violations = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        if re.search(r"[\u4e00-\u9fff]", source):
            violations.append(f"{path}: non-English source text")
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                violations.append(f"{path}:{token.start[0]}: comment")
        for node in ast.walk(tree):
            location = (
                f"{path}:{node.lineno}"
                if isinstance(node, (ast.stmt, ast.expr, ast.arg, ast.keyword, ast.excepthandler))
                else str(path)
            )
            match node:
                case ast.ImportFrom(level=level) if level:
                    violations.append(f"{location}: relative import")
                case ast.Call(func=ast.Name(id=name)) if name in {
                    "getattr",
                    "setattr",
                    "hasattr",
                    "vars",
                }:
                    violations.append(f"{location}: reflective member access")
                case ast.Attribute(attr="__dict__"):
                    violations.append(f"{location}: reflective dictionary access")
                case ast.AnnAssign(annotation=annotation):
                    if re.search(r"\bdict\b|\bJsonObject\b", ast.unparse(annotation)):
                        violations.append(f"{location}: dictionary state instead of a typed record")
                case ast.Call(func=ast.Name(id="TypeVar")):
                    violations.append(f"{location}: TypeVar instead of a type parameter")
                case ast.Return(value=ast.Dict() | ast.DictComp()):
                    violations.append(f"{location}: unmodeled dictionary return")
                case ast.Subscript(value=ast.Name(id="Literal"), slice=values):
                    choices = values.elts if isinstance(values, ast.Tuple) else [values]
                    if any(not isinstance(choice, ast.Attribute) for choice in choices):
                        violations.append(f"{location}: Literal without enum members")
                case ast.FunctionDef() | ast.AsyncFunctionDef():
                    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                    annotations = [argument.annotation for argument in arguments]
                    annotations.append(node.returns)
                    for annotation in annotations:
                        if annotation and re.search(
                            r"\bdict\b|\bJsonObject\b", ast.unparse(annotation)
                        ):
                            violations.append(f"{location}: unmodeled dictionary boundary")
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if ast.get_docstring(node):
                    violations.append(f"{location}: docstring")
    assert not violations, "\n".join(violations)
