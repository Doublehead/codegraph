class Config:
    def save(self): pass


class User:
    def save(self): pass


def uses_config():
    c = Config()
    c.save()        # caller of Config.save only


def uses_user():
    u = User()
    u.save()        # caller of User.save only
