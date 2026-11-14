class PyMongoError(Exception):
    pass


class DuplicateKeyError(PyMongoError):
    pass
