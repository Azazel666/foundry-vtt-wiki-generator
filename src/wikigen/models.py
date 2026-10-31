"""The inventory: what is installed and enabled where."""

from pydantic import BaseModel, Field


class Author(BaseModel):
    name: str = ""
    url: str = ""


class Compatibility(BaseModel):
    minimum: str = ""
    verified: str = ""
    maximum: str = ""


class Pack(BaseModel):
    name: str = ""
    label: str = ""
    type: str = ""


class Package(BaseModel):
    id: str
    title: str = ""
    description: str = ""
    version: str = ""
    authors: list[Author] = Field(default_factory=list)
    url: str = ""
    manifest: str = ""
    download: str = ""
    bugs: str = ""
    readme: str = ""
    changelog: str = ""
    license: str = ""
    compatibility: Compatibility = Field(default_factory=Compatibility)
    library: bool = False
    systems: list[str] = Field(default_factory=list)
    system_compatibility: dict[str, Compatibility] = Field(default_factory=dict)
    requires: list[str] = Field(default_factory=list)
    recommends: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    packs: list[Pack] = Field(default_factory=list)
    folder: str = ""
    mtime: float = 0.0

    @property
    def name(self) -> str:
        return self.title or self.id


class World(BaseModel):
    id: str
    title: str = ""
    description: str = ""
    system: str = ""
    system_version: str = ""
    core_version: str = ""
    last_played: str = ""
    enabled: list[str] = Field(default_factory=list)
    stale: bool = False

    @property
    def name(self) -> str:
        return self.title or self.id


class Broken(BaseModel):
    kind: str
    folder: str
    error: str


class EnvInventory(BaseModel):
    available: bool = True
    error: str = ""
    systems: dict[str, Package] = Field(default_factory=dict)
    modules: dict[str, Package] = Field(default_factory=dict)
    worlds: dict[str, World] = Field(default_factory=dict)
    broken: list[Broken] = Field(default_factory=list)


class Inventory(BaseModel):
    generated_at: str
    envs: dict[str, EnvInventory] = Field(default_factory=dict)
