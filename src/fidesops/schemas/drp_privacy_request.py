from enum import Enum
from typing import Optional, List

from pydantic import validator

from fidesops.models.policy import DrpAction
from fidesops.schemas.base_class import BaseSchema


class DrpMeta(BaseSchema):
    """Enum to hold Drp metadata. Only version is supported at this time"""

    version: str


class DrpRegime(Enum):
    """Enum to hold Drp Regime. Only ccpa supported at this time"""

    ccpa = "ccpa"


class DrpPrivacyRequestCreate(BaseSchema):
    """Data required to create a DRP PrivacyRequest"""

    meta: DrpMeta
    regime: Optional[DrpRegime]
    exercise: List[DrpAction]
    relationships: Optional[List[str]]
    identity: str
    status_callback: Optional[str]

    class Config:
        """Populate models with the raw value of enum fields, rather than the enum itself"""

        use_enum_values = True

    @validator("exercise")
    def check_exercise_length(cls, exercise: [List[DrpAction]]) -> List[DrpAction]:
        """Validate the only one exercise action is provided"""
        if len(exercise) > 1:
            raise ValueError("Multiple exercise actions are not supported at this time")
        return exercise


class DrpIdentity(BaseSchema):
    """Drp identity props"""

    aud: Optional[str]
    sub: Optional[str]
    name: Optional[str]
    email: Optional[str]
    email_verified: Optional[bool]
    phone_number: Optional[str]
    phone_number_verified: Optional[bool]
    address: Optional[str]
    address_verified: Optional[bool]
    owner_of_attorney: Optional[str]
