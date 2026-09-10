"""Strict version-1 authoring models.

The models deliberately describe authoring data only. Filesystem and
cross-document rules live in :mod:`startergen.validate` so that parsing a
model never silently touches or changes the project.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectConfig(StrictModel):
    name: str = Field(min_length=1)
    import_package: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def name_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("import_package")
    @classmethod
    def import_package_is_valid(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", value):
            raise ValueError("must be a dotted Python import package name")
        return value


class StarterConfig(StrictModel):
    output: str = Field(min_length=1)
    include: list[str] = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)


class DocumentationConfig(StrictModel):
    source: str = Field(min_length=1)
    assets: str = Field(min_length=1)
    background: str = Field(min_length=1)
    readme_template: str = Field(min_length=1)
    generated_source: str = Field(min_length=1)
    site_output: str = Field(min_length=1)


class PublicationConfig(StrictModel):
    starter_repository: str = Field(min_length=3)
    branch: str = Field(default="main", min_length=1)
    docs_base_url: str

    @field_validator("starter_repository")
    @classmethod
    def repository_is_owner_name(cls, value: str) -> str:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", value):
            raise ValueError("must use OWNER/REPOSITORY form")
        return value

    @field_validator("docs_base_url")
    @classmethod
    def docs_url_is_absolute(cls, value: str) -> str:
        try:
            HttpUrl(value)
        except Exception as exc:
            raise ValueError("must be an absolute HTTP(S) URL") from exc
        return value


class ProjectMetadata(StrictModel):
    schema_version: Literal[1]
    project: ProjectConfig
    starter: StarterConfig
    documentation: DocumentationConfig
    publication: PublicationConfig | None = None


class ExerciseSource(StrictModel):
    file: str = Field(min_length=1)
    symbol: str = Field(min_length=1)


class StarterSpec(StrictModel):
    strategy: Literal["replace_body"]
    docstring: Literal["preserve", "drop"]
    body: str | None = None


class BaselineTest(StrictModel):
    nodeid: str = Field(min_length=1)
    expected: Literal["stub_error", "assertion_failure"]
    assertion_message: str | None = None

    @model_validator(mode="after")
    def validate_expected_details(self) -> BaselineTest:
        if self.expected == "assertion_failure" and not self.assertion_message:
            raise ValueError("assertion_message is required for assertion_failure")
        if self.expected == "stub_error" and self.assertion_message is not None:
            raise ValueError("assertion_message is only valid for assertion_failure")
        return self


class ExerciseTests(StrictModel):
    public: list[str] = Field(min_length=1)
    baseline: list[BaselineTest] = Field(min_length=1)


class Exercise(StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source: ExerciseSource
    requires: list[str] = Field(default_factory=list)
    starter: StarterSpec
    tests: ExerciseTests

    @field_validator("id")
    @classmethod
    def id_is_stable(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("must use lowercase kebab-case")
        return value


class ExerciseMetadata(StrictModel):
    schema_version: Literal[1]
    exercises: list[Exercise] = Field(min_length=1)
