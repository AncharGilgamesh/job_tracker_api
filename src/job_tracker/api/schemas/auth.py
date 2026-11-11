"""Registration and login payloads."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RegistrationRequest(BaseModel):
    """Email and password as supplied. Normalization happens in the service."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    email: str
    password: str


class RegistrationResponse(BaseModel):
    """The only public registration result."""

    message: str


class LoginRequest(BaseModel):
    """Email and password as supplied. Normalization happens in the service."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    email: str
    password: str


class LoginResponse(BaseModel):
    """Access token returned after a committed login. No refresh token."""

    access_token: str
    token_type: Literal["bearer"]
    expires_in: Literal[900]
