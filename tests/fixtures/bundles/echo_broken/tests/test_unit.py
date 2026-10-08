from capability import run


def test_echo():
    assert run("ahoj")["echo"] == "ahoj"
