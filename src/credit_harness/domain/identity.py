"""Public reference vocabulary; no raw identity records or resolvers."""
from typing import Annotated
from pydantic import Field

CustomerRef = Annotated[str, Field(strict=True, pattern=r"^CUS-[A-Za-z0-9-]{1,64}$")]
BeneficiaryRef = Annotated[str, Field(strict=True, pattern=r"^BEN-[A-Za-z0-9-]{1,64}$")]
AccountRef = Annotated[str, Field(strict=True, pattern=r"^ACC-[A-Za-z0-9-]{1,64}$")]
