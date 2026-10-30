import telegraph


def test_md_to_nodes_parses_core_blocks_and_inline_markup():
    nodes = telegraph.md_to_nodes(
        "\n".join([
            "# Title **bold**",
            "",
            "First line with `code`",
            "continues with [link](https://example.test).",
            "",
            "> quote one",
            "> quote *two*",
            "",
            "- item **one**",
            "- item two",
            "",
            "1. first",
            "2. second",
            "",
            "---",
            "",
            "```python",
            "print('hi')",
            "```",
        ])
    )

    assert nodes == [
        {"tag": "h3", "children": ["Title ", {"tag": "b", "children": ["bold"]}]},
        {
            "tag": "p",
            "children": [
                "First line with ",
                {"tag": "code", "children": ["code"]},
                " continues with ",
                {"tag": "a", "attrs": {"href": "https://example.test"}, "children": ["link"]},
                ".",
            ],
        },
        {"tag": "blockquote", "children": ["quote one\nquote ", {"tag": "i", "children": ["two"]}]},
        {
            "tag": "ul",
            "children": [
                {"tag": "li", "children": ["item ", {"tag": "b", "children": ["one"]}]},
                {"tag": "li", "children": ["item two"]},
            ],
        },
        {
            "tag": "ol",
            "children": [
                {"tag": "li", "children": ["first"]},
                {"tag": "li", "children": ["second"]},
            ],
        },
        {"tag": "hr"},
        {
            "tag": "pre",
            "children": [
                {
                    "tag": "code",
                    "attrs": {"class": "language-python"},
                    "children": ["print('hi')"],
                }
            ],
        },
    ]


def test_md_to_nodes_handles_unclosed_plain_code_fence():
    assert telegraph.md_to_nodes("```\na\nb") == [
        {"tag": "pre", "children": ["a\nb"]}
    ]
