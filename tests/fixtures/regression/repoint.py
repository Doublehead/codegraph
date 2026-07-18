class Foo:
    def save(self):
        return 1


class Bar:
    def save(self):
        return 2


def consumer():
    x = Foo()
    return x.save()
