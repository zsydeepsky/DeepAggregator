from app.core.services.fulltext import html_to_text


def test_html_to_text_strips_scripts_and_keeps_content():
    html = (
        "<html><head><style>.x{color:red}</style></head><body>"
        "<nav>菜单</nav><div><h1>深空探测</h1><script>evil()</script>"
        "<p>正文第一段</p><p>正文第二段</p></div></body></html>"
    )
    text = html_to_text(html)
    assert "深空探测" in text
    assert "正文第一段" in text
    assert "evil" not in text
    assert "菜单" in text
