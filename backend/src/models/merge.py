from sqlalchemy import JSON,ForeignKey
from sqlalchemy.orm import Mapped,mapped_column
from ..config.database import Base
from ..utils.clock import now


class StudyBaseline(Base):
    """Immutable merge baseline for one committed study revision.

    ``records`` is the canonical window list of that revision.  The common
    ancestor for a three-way merge is the baseline named by the importing
    package, so branches stay reproducible even after later merges.
    """
    __tablename__="study_baselines"
    id:Mapped[int]=mapped_column(primary_key=True)
    study_id:Mapped[int]=mapped_column(ForeignKey("archive_studies.id"),index=True)
    revision:Mapped[int]
    base_hash:Mapped[str]
    branch:Mapped[str]=mapped_column(default="main")
    records:Mapped[list]=mapped_column(JSON,default=list)
    created_at:Mapped[str]=mapped_column(default=now)


class SourceMergePackage(Base):
    """One imported branch package and its merge verdict.

    ``base_records`` is what the exporting side saw; ``records`` is what it
    wants.  A package is applied whole or not at all: while it is
    ``conflict`` the live study is untouched, and a single admin resolution
    moves it to ``merged`` / ``rejected``.
    """
    __tablename__="source_merge_packages"
    id:Mapped[int]=mapped_column(primary_key=True)
    package_uid:Mapped[str]=mapped_column(unique=True)
    study_id:Mapped[int]=mapped_column(ForeignKey("archive_studies.id"),index=True)
    branch:Mapped[str]
    base_revision:Mapped[int]
    base_hash:Mapped[str]
    content_hash:Mapped[str]
    state_hash:Mapped[str]=mapped_column(default="")
    base_records:Mapped[list]=mapped_column(JSON,default=list)
    records:Mapped[list]=mapped_column(JSON,default=list)
    status:Mapped[str]
    summary:Mapped[dict]=mapped_column(JSON,default=dict)
    conflicts:Mapped[list]=mapped_column(JSON,default=list)
    resolution:Mapped[str]=mapped_column(default="")
    resolved_by:Mapped[str]=mapped_column(default="")
    resolved_at:Mapped[str]=mapped_column(default="")
    imported_by:Mapped[str]=mapped_column(default="")
    created_at:Mapped[str]=mapped_column(default=now)
