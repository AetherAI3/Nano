"""Recursive-descent parser for `.nano` (v1.0 grammar).

Invariant: a StrategyAst that leaves this module is well-formed — every block is
closed, every action is known, every literal is in range — because this is the
last place a violation can still be reported with the exact 1-based line/column
of the offending token. Meaning is somebody else's job: names, types, arity, and
look-ahead belong to ``nano/types/checker.py``, which runs next.

The v1.0 grammar is a superset of v0.1.0. Everything that compiled before parses
to the same AST, so the example corpus and strategy library are untouched; what
v1.0 adds is declarations, real expressions, and control flow.

Grammar (locked, v1.0):

    program     = [ tierDecl ] , strategy ;
    tierDecl    = "tier" , ( "nano" | "nano+" | "nano++" ) ;
    strategy    = "strategy" , IDENT , "{" , member* , "}" ;

    member      = paramDecl | inputDecl | letDecl | riskBlock
                | signatureDecl | routeBlock | agentDecl | scheduleBlock ;
    paramDecl   = "param" , IDENT , [ ":" , type ] , "=" , literal ;
    inputDecl   = "input" , IDENT , ":" , type ;
    letDecl     = "let" , IDENT , [ ":" , type ] , "=" , expr ;
    riskBlock   = "risk" , "{" , { IDENT , number } , "}" ;
    signatureDecl = "signature" , IDENT , "{" , sigField+ , "}" ;
    sigField    = ( "input" | "output" ) , IDENT , ":" , type ,
                  [ "range" , "[" , number , "," , number , "]" ] ;
    routeBlock  = "route" , IDENT , "{" , "execute" , IDENT , [ "when" ] , expr ,
                  "otherwise" , "{" , escalate , "}" , "}" ;
    agentDecl   = "agent" , IDENT , [ "{" , "role" , IDENT , "}" ] ;
    scheduleBlock = "every" , INTERVAL , "{" , statement* , "}" ;

    statement   = ifStmt | escalate | action ;
    ifStmt      = "if" , expr , "{" , statement* , "}" ,
                  [ "else" , "{" , statement* , "}" ] ;
    escalate    = "escalate" , ( STRING | IDENT ) ;
    action      = ( "buy" | "sell" ) , "(" , IDENT , [ "," , number ] , ")"
                | ( "execute" | "pause" | "observe" ) , "(" , ")" ;

    expr        = orExpr ;
    orExpr      = andExpr , { "or" , andExpr } ;
    andExpr     = notExpr , { "and" , notExpr } ;
    notExpr     = [ "not" ] , comparison ;
    comparison  = additive , [ ( "<" | "<=" | ">" | ">=" | "==" | "!=" ) , additive ] ;
    additive    = multiplicative , { ( "+" | "-" ) , multiplicative } ;
    multiplicative = unary , { ( "*" | "/" | "%" ) , unary } ;
    unary       = [ "-" ] , postfix ;
    postfix     = primary , { "[" , expr , "]" | "." , IDENT } ;
    primary     = number | STRING | "true" | "false" | INTERVAL
                | IDENT , [ "(" , [ expr , { "," , expr } ] , ")" ]
                | "(" , expr , ")" ;

Comparisons do not chain: `a < b < c` is rejected rather than silently parsed as
`(a < b) < c`, which in a strategy would compare a boolean against a price.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .ast import (
    ActionAst,
    AgentAst,
    Binary,
    BoolLit,
    Call,
    ConditionAst,
    DurationLit,
    EscalateStmt,
    Expr,
    IfStmt,
    Index,
    InputAst,
    LetAst,
    Literal,
    Member,
    Name,
    Number,
    NumberLit,
    ParamAst,
    RiskAst,
    RiskLimitAst,
    RouteAst,
    RuleAst,
    ScheduleAst,
    SigFieldAst,
    SignatureAst,
    Stmt,
    StrategyAst,
    StringLit,
    Unary,
)
from ..ir.schema import CONDITION_OPERATORS
from .errors import NanoSyntaxError
from .lexer import decode_string, tokenize
from .tokens import Token

# Surface action name -> IR intent action. buy/sell take (asset[, confidence]);
# the rest take no arguments.
_ASSET_ACTIONS = {"buy": "BUY", "sell": "SELL"}
_NULLARY_ACTIONS = {"execute": "EXECUTE", "pause": "PAUSE", "observe": "OBSERVE"}
_ALL_ACTIONS = {**_ASSET_ACTIONS, **_NULLARY_ACTIONS}

# One source for the operator vocabulary: the IR schema, which is what a
# `Condition` node may actually carry.
_COMPARISON_OPS = CONDITION_OPERATORS
_ADDITIVE_OPS = frozenset({"+", "-"})
_MULTIPLICATIVE_OPS = frozenset({"*", "/", "%"})

_MEMBER_KEYWORDS = (
    "param",
    "input",
    "let",
    "risk",
    "signature",
    "route",
    "agent",
    "every",
)


class _Parser:
    def __init__(self, tokens: Tuple[Token, ...]) -> None:
        self._tokens = tokens
        self._pos = 0
        # Set when a `>=` token had to be split into `>` (closing a type) plus a
        # leftover `=`. See _close_type_bracket.
        self._pending: Optional[Token] = None

    # -- token plumbing ----------------------------------------------------

    def _peek(self) -> Token:
        if self._pending is not None:
            return self._pending
        return self._tokens[self._pos]

    def _advance(self) -> Token:
        if self._pending is not None:
            token = self._pending
            self._pending = None
            return token
        token = self._tokens[self._pos]
        if token.type != "EOF":
            self._pos += 1
        return token

    @staticmethod
    def _error(message: str, token: Token) -> NanoSyntaxError:
        return NanoSyntaxError(message, token.line, token.column)

    @staticmethod
    def _describe(token: Token) -> str:
        return "end of input" if token.type == "EOF" else repr(token.value)

    def _expect(self, token_type: str, what: str) -> Token:
        token = self._peek()
        if token.type != token_type:
            raise self._error(f"Expected {what}, got {self._describe(token)}", token)
        return self._advance()

    def _expect_keyword(self, keyword: str) -> Token:
        token = self._peek()
        if token.type != "IDENT" or token.value != keyword:
            raise self._error(
                f"Expected {keyword!r} keyword, got {self._describe(token)}", token
            )
        return self._advance()

    def _at_keyword(self, keyword: str) -> bool:
        token = self._peek()
        return token.type == "IDENT" and token.value == keyword

    def _at_op(self, *values: str) -> bool:
        token = self._peek()
        return token.type == "OP" and token.value in values

    def _expect_closing_brace(self, block: str) -> None:
        token = self._peek()
        if token.type == "EOF":
            raise self._error(f"Unterminated {block} block (missing '}}')", token)
        if token.type != "RBRACE":
            raise self._error(
                f"Unexpected token {self._describe(token)} in {block} block", token
            )
        self._advance()

    # -- program -----------------------------------------------------------

    def parse_program(self) -> StrategyAst:
        tier = "nano"
        if self._at_keyword("tier"):
            self._advance()
            tier = self._parse_tier()

        header = self._expect_keyword("strategy")
        name = self._expect("IDENT", "strategy name").value
        self._expect("LBRACE", "'{'")

        params: List[ParamAst] = []
        inputs: List[InputAst] = []
        lets: List[LetAst] = []
        signatures: List[SignatureAst] = []
        routes: List[RouteAst] = []
        agents: List[AgentAst] = []
        schedules: List[ScheduleAst] = []
        risk: Optional[RiskAst] = None

        while True:
            token = self._peek()
            if token.type == "RBRACE":
                self._advance()
                break
            if token.type == "EOF":
                raise self._error("Unterminated 'strategy' block (missing '}')", token)

            if self._at_keyword("param"):
                params.append(self._parse_param())
            elif self._at_keyword("input"):
                inputs.append(self._parse_input())
            elif self._at_keyword("let"):
                lets.append(self._parse_let())
            elif self._at_keyword("risk"):
                if risk is not None:
                    raise self._error(
                        "At most one risk block is allowed per strategy", token
                    )
                risk = self._parse_risk()
            elif self._at_keyword("signature"):
                signatures.append(self._parse_signature())
            elif self._at_keyword("route"):
                routes.append(self._parse_route())
            elif self._at_keyword("agent"):
                agents.append(self._parse_agent())
            elif self._at_keyword("every"):
                schedules.append(self._parse_schedule())
            else:
                raise self._error(
                    f"Unexpected token {self._describe(token)} in 'strategy' block "
                    f"(expected one of {', '.join(_MEMBER_KEYWORDS)})",
                    token,
                )

        trailing = self._peek()
        if trailing.type != "EOF":
            raise self._error(
                f"Unexpected token {self._describe(trailing)} after strategy block",
                trailing,
            )
        return StrategyAst(
            name=name,
            tier=tier,
            params=tuple(params),
            inputs=tuple(inputs),
            lets=tuple(lets),
            risk=risk,
            signatures=tuple(signatures),
            routes=tuple(routes),
            agents=tuple(agents),
            schedules=tuple(schedules),
            line=header.line,
            column=header.column,
        )

    def _parse_tier(self) -> str:
        """Read `nano`, `nano+`, or `nano++`.

        The lexer has no idea `nano++` is one word — it sees an identifier and
        two `+` operators — so the tier is reassembled here.
        """
        base = self._expect("IDENT", "tier name (nano, nano+, or nano++)")
        tier = base.value
        while self._at_op("+"):
            self._advance()
            tier += "+"
        return tier

    # -- declarations ------------------------------------------------------

    def _parse_param(self) -> ParamAst:
        keyword = self._expect_keyword("param")
        name = self._expect("IDENT", "param name").value
        declared = self._parse_optional_annotation()
        self._expect("ASSIGN", "'=' followed by a default value")
        value = self._parse_literal()
        return ParamAst(
            name=name,
            declared_type=declared,
            value=value,
            line=keyword.line,
            column=keyword.column,
        )

    def _parse_input(self) -> InputAst:
        keyword = self._expect_keyword("input")
        name = self._expect("IDENT", "input name").value
        self._expect("COLON", "':' followed by a type")
        declared = self._parse_type()
        return InputAst(
            name=name,
            declared_type=declared,
            line=keyword.line,
            column=keyword.column,
        )

    def _parse_let(self) -> LetAst:
        keyword = self._expect_keyword("let")
        name = self._expect("IDENT", "binding name").value
        declared = self._parse_optional_annotation()
        self._expect("ASSIGN", "'=' followed by an expression")
        expr = self._parse_expr()
        return LetAst(
            name=name,
            declared_type=declared,
            expr=expr,
            line=keyword.line,
            column=keyword.column,
        )

    def _parse_risk(self) -> RiskAst:
        keyword = self._expect_keyword("risk")
        self._expect("LBRACE", "'{'")
        limits: List[RiskLimitAst] = []
        while True:
            token = self._peek()
            if token.type == "RBRACE":
                self._advance()
                break
            if token.type == "EOF":
                raise self._error("Unterminated 'risk' block (missing '}')", token)
            name_token = self._expect("IDENT", "risk limit name")
            value = self._parse_number("risk limit value")
            limits.append(
                RiskLimitAst(
                    name=name_token.value,
                    value=value,
                    line=name_token.line,
                    column=name_token.column,
                )
            )
        return RiskAst(limits=tuple(limits), line=keyword.line, column=keyword.column)

    def _parse_signature(self) -> SignatureAst:
        keyword = self._expect_keyword("signature")
        name = self._expect("IDENT", "signature name").value
        self._expect("LBRACE", "'{'")
        inputs: List[SigFieldAst] = []
        outputs: List[SigFieldAst] = []
        while True:
            token = self._peek()
            if token.type == "RBRACE":
                self._advance()
                break
            if token.type == "EOF":
                raise self._error(
                    "Unterminated 'signature' block (missing '}')", token
                )
            if self._at_keyword("input"):
                self._advance()
                inputs.append(self._parse_signature_field())
            elif self._at_keyword("output"):
                self._advance()
                outputs.append(self._parse_signature_field())
            else:
                raise self._error(
                    f"Unexpected token {self._describe(token)} in 'signature' "
                    "block (expected 'input' or 'output')",
                    token,
                )
        if not outputs:
            raise self._error(
                f"Signature {name!r} declares no outputs — a reasoning call with "
                "no typed result has nothing a strategy can act on",
                keyword,
            )
        return SignatureAst(
            name=name,
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            line=keyword.line,
            column=keyword.column,
        )

    def _parse_signature_field(self) -> SigFieldAst:
        name_token = self._expect("IDENT", "field name")
        self._expect("COLON", "':' followed by a type")
        declared = self._parse_type()
        low: Optional[Number] = None
        high: Optional[Number] = None
        if self._at_keyword("range"):
            self._advance()
            self._expect("LBRACKET", "'['")
            low = self._parse_number("range lower bound")
            self._expect("COMMA", "','")
            high = self._parse_number("range upper bound")
            self._expect("RBRACKET", "']'")
        return SigFieldAst(
            name=name_token.value,
            declared_type=declared,
            low=low,
            high=high,
            line=name_token.line,
            column=name_token.column,
        )

    def _parse_route(self) -> RouteAst:
        keyword = self._expect_keyword("route")
        name = self._expect("IDENT", "route name").value
        self._expect("LBRACE", "'{'")
        self._expect_keyword("execute")
        execute = self._expect("IDENT", "the name this route executes").value
        # `when` is optional: the locked platform spec writes the guard on its own
        # line, while spelling it out reads better inline. Both mean the same.
        if self._at_keyword("when"):
            self._advance()
        when = self._parse_expr()
        self._expect_keyword("otherwise")
        self._expect("LBRACE", "'{'")
        otherwise = self._parse_escalate()
        self._expect_closing_brace("'otherwise'")
        self._expect_closing_brace("'route'")
        return RouteAst(
            name=name,
            execute=execute,
            when=when,
            otherwise=otherwise,
            line=keyword.line,
            column=keyword.column,
        )

    def _parse_agent(self) -> AgentAst:
        keyword = self._expect_keyword("agent")
        name_token = self._expect("IDENT", "agent name")
        role: Optional[str] = None
        # A brace here can only open an agent body: every other member starts
        # with its own keyword, so there is nothing to disambiguate against.
        if self._peek().type == "LBRACE":
            self._advance()
            token = self._peek()
            if token.type == "RBRACE":
                raise self._error(
                    f"Agent {name_token.value!r} body requires exactly one "
                    "'role' clause (omit the braces for an unclassified agent)",
                    token,
                )
            if token.type == "EOF":
                raise self._error("Unterminated 'agent' block (missing '}')", token)

            self._expect_keyword("role")
            role = self._expect("IDENT", "agent role").value

            token = self._peek()
            if self._at_keyword("role"):
                raise self._error(
                    f"Agent {name_token.value!r} declares 'role' more than once",
                    token,
                )
            self._expect_closing_brace("'agent'")
        return AgentAst(
            name=name_token.value,
            role=role,
            line=keyword.line,
            column=keyword.column,
        )

    # -- schedules and statements ------------------------------------------

    def _parse_schedule(self) -> ScheduleAst:
        keyword = self._expect_keyword("every")
        interval = self._expect("INTERVAL", "interval (e.g. 5m, 1h)").value
        self._expect("LBRACE", "'{'")
        statements = self._parse_block_statements("'every'")
        return ScheduleAst(
            interval=interval,
            rules=self._group_into_rules(statements),
            line=keyword.line,
            column=keyword.column,
        )

    def _group_into_rules(self, statements: Tuple[Stmt, ...]) -> Tuple[RuleAst, ...]:
        """Turn a schedule body into rules, preserving source order.

        An `if` becomes a guarded rule. A run of bare statements becomes one
        unconditional rule — `every 5m { observe() }` should mean "observe every
        bar", and making the author write `if true` to say that would be noise.
        """
        rules: List[RuleAst] = []
        pending: List[Stmt] = []

        def flush() -> None:
            if not pending:
                return
            first = pending[0]
            rules.append(
                RuleAst(
                    when=BoolLit(line=first.line, column=first.column, value=True),
                    then=tuple(pending),
                    otherwise=(),
                    line=first.line,
                    column=first.column,
                )
            )
            pending.clear()

        for statement in statements:
            if isinstance(statement, IfStmt):
                flush()
                rules.append(
                    RuleAst(
                        when=statement.when,
                        then=statement.then,
                        otherwise=statement.otherwise,
                        line=statement.line,
                        column=statement.column,
                    )
                )
                continue
            pending.append(statement)
        flush()
        return tuple(rules)

    def _parse_block_statements(self, block: str) -> Tuple[Stmt, ...]:
        statements: List[Stmt] = []
        while True:
            token = self._peek()
            if token.type == "RBRACE":
                self._advance()
                return tuple(statements)
            if token.type == "EOF":
                raise self._error(f"Unterminated {block} block (missing '}}')", token)
            statements.append(self._parse_statement())

    def _parse_statement(self) -> Stmt:
        if self._at_keyword("if"):
            return self._parse_if()
        if self._at_keyword("escalate"):
            return self._parse_escalate()
        return self._parse_action()

    def _parse_if(self) -> IfStmt:
        keyword = self._expect_keyword("if")
        when = self._parse_expr()
        self._expect("LBRACE", "'{'")
        then = self._parse_block_statements("'if'")
        otherwise: Tuple[Stmt, ...] = ()
        if self._at_keyword("else"):
            self._advance()
            self._expect("LBRACE", "'{'")
            otherwise = self._parse_block_statements("'else'")
        return IfStmt(
            line=keyword.line,
            column=keyword.column,
            when=when,
            then=then,
            otherwise=otherwise,
        )

    def _parse_escalate(self) -> EscalateStmt:
        keyword = self._expect_keyword("escalate")
        token = self._peek()
        if token.type == "STRING":
            self._advance()
            return EscalateStmt(
                line=keyword.line,
                column=keyword.column,
                target=decode_string(token.value),
                is_name=False,
            )
        name = self._expect("IDENT", "an agent name or a quoted target")
        return EscalateStmt(
            line=keyword.line,
            column=keyword.column,
            target=name.value,
            is_name=True,
        )

    def _parse_action(self) -> ActionAst:
        name_token = self._expect("IDENT", "action name")
        surface_name = name_token.value
        if surface_name not in _ALL_ACTIONS:
            raise self._error(
                f"Unknown action {surface_name!r} "
                "(expected one of buy, sell, execute, pause, observe)",
                name_token,
            )
        action = _ALL_ACTIONS[surface_name]
        self._expect("LPAREN", "'('")

        if surface_name in _NULLARY_ACTIONS:
            self._expect("RPAREN", "')'")
            return ActionAst(
                line=name_token.line, column=name_token.column, action=action
            )

        asset = self._expect("IDENT", "asset name").value
        confidence: Optional[Number] = None
        if self._peek().type == "COMMA":
            self._advance()
            confidence_token = self._peek()
            confidence = self._parse_number("confidence value")
            if not 0 <= confidence <= 1:
                raise self._error(
                    f"Confidence {confidence} is out of range [0, 1]",
                    confidence_token,
                )
        self._expect("RPAREN", "')'")
        return ActionAst(
            line=name_token.line,
            column=name_token.column,
            action=action,
            asset=asset,
            confidence=confidence,
        )

    # -- types -------------------------------------------------------------

    def _parse_optional_annotation(self) -> Optional[str]:
        if self._peek().type != "COLON":
            return None
        self._advance()
        return self._parse_type()

    def _parse_type(self) -> str:
        """Parse a type into its canonical spelling, e.g. `series<float>`."""
        head = self._expect("IDENT", "type name")
        if not self._at_op("<"):
            return head.value
        self._advance()
        element = self._expect("IDENT", "element type").value
        self._close_type_bracket()
        return f"{head.value}<{element}>"

    def _close_type_bracket(self) -> None:
        """Consume the `>` closing a generic type.

        `series<float>=EMA(...)` lexes the `>=` as one operator, the same way C++
        once mis-lexed `vector<vector<int>>`. Splitting it here — taking the `>`
        and leaving an `=` for the caller — means the author does not have to
        remember a space before the equals sign.
        """
        token = self._peek()
        if token.type == "OP" and token.value == ">":
            self._advance()
            return
        if token.type == "OP" and token.value == ">=":
            self._advance()
            self._pending = Token("ASSIGN", "=", token.line, token.column + 1)
            return
        raise self._error(
            f"Expected '>' to close the type, got {self._describe(token)}", token
        )

    # -- expressions -------------------------------------------------------

    def _parse_expr(self) -> Expr:
        return self._parse_or()

    def _parse_or(self) -> Expr:
        left = self._parse_and()
        while self._at_keyword("or"):
            token = self._advance()
            right = self._parse_and()
            left = Binary(
                line=token.line, column=token.column, op="or", left=left, right=right
            )
        return left

    def _parse_and(self) -> Expr:
        left = self._parse_not()
        while self._at_keyword("and"):
            token = self._advance()
            right = self._parse_not()
            left = Binary(
                line=token.line, column=token.column, op="and", left=left, right=right
            )
        return left

    def _parse_not(self) -> Expr:
        if self._at_keyword("not"):
            token = self._advance()
            return Unary(
                line=token.line,
                column=token.column,
                op="not",
                operand=self._parse_not(),
            )
        return self._parse_comparison()

    def _parse_comparison(self) -> Expr:
        left = self._parse_additive()

        token = self._peek()
        if token.type == "ASSIGN":
            # `=` can never legally follow an expression here, and a lone `=` in a
            # condition is always a mistyped `==`. Naming the operator that was
            # expected beats a downstream "expected '{'" pointing somewhere else.
            raise self._error(
                "Expected comparison operator (one of <, <=, >, >=, ==, !=), "
                f"got {self._describe(token)}",
                token,
            )
        if not self._at_op(*_COMPARISON_OPS):
            return left

        operator = self._advance()
        right = self._parse_additive()
        result = Binary(
            line=operator.line,
            column=operator.column,
            op=operator.value,
            left=left,
            right=right,
        )

        if self._at_op(*_COMPARISON_OPS):
            chained = self._peek()
            raise self._error(
                f"Comparisons do not chain: {chained.value!r} would compare a "
                "boolean against a value. Split it with 'and'",
                chained,
            )
        return result

    def _parse_additive(self) -> Expr:
        left = self._parse_multiplicative()
        while self._at_op(*_ADDITIVE_OPS):
            operator = self._advance()
            right = self._parse_multiplicative()
            left = Binary(
                line=operator.line,
                column=operator.column,
                op=operator.value,
                left=left,
                right=right,
            )
        return left

    def _parse_multiplicative(self) -> Expr:
        left = self._parse_unary()
        while self._at_op(*_MULTIPLICATIVE_OPS):
            operator = self._advance()
            right = self._parse_unary()
            left = Binary(
                line=operator.line,
                column=operator.column,
                op=operator.value,
                left=left,
                right=right,
            )
        return left

    def _parse_unary(self) -> Expr:
        if self._at_op("-"):
            token = self._advance()
            return Unary(
                line=token.line,
                column=token.column,
                op="-",
                operand=self._parse_unary(),
            )
        return self._parse_postfix()

    def _parse_postfix(self) -> Expr:
        expr = self._parse_primary()
        while True:
            token = self._peek()
            if token.type == "LBRACKET":
                self._advance()
                offset = self._parse_expr()
                self._expect("RBRACKET", "']'")
                expr = Index(
                    line=token.line, column=token.column, target=expr, offset=offset
                )
                continue
            if token.type == "DOT":
                self._advance()
                field = self._expect("IDENT", "field name")
                expr = Member(
                    line=token.line,
                    column=token.column,
                    target=expr,
                    field_name=field.value,
                )
                continue
            return expr

    def _parse_primary(self) -> Expr:
        token = self._peek()

        if token.type in ("INT", "FLOAT"):
            self._advance()
            return NumberLit(
                line=token.line,
                column=token.column,
                value=int(token.value) if token.type == "INT" else float(token.value),
            )
        if token.type == "STRING":
            self._advance()
            return StringLit(
                line=token.line,
                column=token.column,
                value=decode_string(token.value),
            )
        if token.type == "INTERVAL":
            self._advance()
            return DurationLit(line=token.line, column=token.column, text=token.value)
        if token.type == "LPAREN":
            self._advance()
            inner = self._parse_expr()
            self._expect("RPAREN", "')'")
            return inner
        if token.type == "IDENT":
            if token.value in ("true", "false"):
                self._advance()
                return BoolLit(
                    line=token.line, column=token.column, value=token.value == "true"
                )
            self._advance()
            if self._peek().type != "LPAREN":
                return Name(line=token.line, column=token.column, name=token.value)
            self._advance()
            args: List[Expr] = []
            if self._peek().type != "RPAREN":
                args.append(self._parse_expr())
                while self._peek().type == "COMMA":
                    self._advance()
                    args.append(self._parse_expr())
            self._expect("RPAREN", "')'")
            return Call(
                line=token.line,
                column=token.column,
                callee=token.value,
                args=tuple(args),
            )

        raise self._error(f"Expected an expression, got {self._describe(token)}", token)

    # -- literals ----------------------------------------------------------

    def _parse_literal(self) -> Literal:
        token = self._peek()
        if token.type == "STRING":
            self._advance()
            return decode_string(token.value)
        if token.type == "IDENT" and token.value in ("true", "false"):
            self._advance()
            return token.value == "true"
        return self._parse_number("literal value")

    def _parse_number(self, what: str) -> Number:
        negative = False
        if self._at_op("-"):
            self._advance()
            negative = True
        token = self._peek()
        if token.type == "INT":
            self._advance()
            value: Number = int(token.value)
        elif token.type == "FLOAT":
            self._advance()
            value = float(token.value)
        else:
            raise self._error(
                f"Expected numeric {what}, got {self._describe(token)}", token
            )
        return -value if negative else value


def parse(source: str) -> StrategyAst:
    """Parse `.nano` source into an immutable, well-formed AST."""
    return _Parser(tokenize(source)).parse_program()


__all__ = [
    "ActionAst",
    "AgentAst",
    "ConditionAst",
    "EscalateStmt",
    "IfStmt",
    "InputAst",
    "LetAst",
    "ParamAst",
    "RiskAst",
    "RiskLimitAst",
    "RouteAst",
    "RuleAst",
    "ScheduleAst",
    "SigFieldAst",
    "SignatureAst",
    "StrategyAst",
    "parse",
]
