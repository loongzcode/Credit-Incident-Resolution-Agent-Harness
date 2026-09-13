"""DDL types frozen with the initial migration; no application dependency."""
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType):
    cache_ok = True
    def get_col_spec(self, **kwargs):
        return 'vector'
