import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_user() -> str:
    """Try $USER first, then $LOGNAME, then 'anonymous'."""
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "anonymous"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EMBRYODB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    source_dir: Path = Field(
        default=Path("/murrlab/gpfs/fs0/l/murr/embryoDB"),
        description="Read-only source XML directory. Never written to.",
    )
    export_dir: Path = Field(
        default=Path("/murrlab/gpfs/fs0/l/murr/new_tools/embryoDB_exports"),
        description="Where DB->XML writes land. Retires when promote-to-source is implemented.",
    )
    db_url: str = Field(
        default="postgresql+psycopg://embryodb@localhost/embryodb",
        description="SQLAlchemy URL. SQLite path acceptable for local dev/tests.",
    )
    user: str = Field(
        default_factory=_default_user,
        description=(
            "Identifier recorded in updated_by / imported_by columns. "
            "Defaults to $USER / $LOGNAME, falls back to 'anonymous'."
        ),
    )

    # AceTree external launcher (legacy Java GUI). Used by the detail panel's
    # "Launch AceTree" button. Mirrors the behaviour of the old EmbryoDB.jar
    # "acetreex" action: `java -mx500m -jar <jar> <annot_loc>/dats/<config>`.
    acetree_jar: Path = Field(
        default=Path("/gpfs/fs0/l/murr/tools3/AceTree_Santella.jar"),
        description="Path to the AceTree jar to spawn for legacy curation.",
    )
    java_command: str = Field(
        default="java",
        description="Java launcher binary (overridable if multiple JREs are present).",
    )
    java_mx: str = Field(
        default="500m",
        description="JVM max-heap argument forwarded to AceTree (-mx<value>).",
    )

    # Pipeline subprocess tools
    tools3_dir: Path = Field(
        default=Path("/gpfs/fs0/l/murr/tools3"),
        description="Directory containing matlab_SN_cluster.pl, acebatch3.jar, etc.",
    )
    worker_pidfile_dir: Path = Field(
        default=Path("/tmp"),
        description="Directory for the per-machine worker pidfile.",
    )


settings = Settings()
