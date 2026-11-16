from sqlalchemy import Text,JSON,ForeignKey,Double,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from ..config.database import Base
from ..utils.clock import now
class ClimateWindow(Base):
    __tablename__="climate_windows"
    id:Mapped[int]=mapped_column(primary_key=True)
    __table_args__=(UniqueConstraint("study_id","code"),UniqueConstraint("study_id","origin_uid"))
    study_id:Mapped[int]=mapped_column(ForeignKey("archive_studies.id"))
    origin_uid:Mapped[str|None]=mapped_column(default=None,index=True)
    code:Mapped[str]
    split:Mapped[str]
    values:Mapped[dict]=mapped_column(JSON)
    note:Mapped[str]=mapped_column(Text)
    revision:Mapped[int]=mapped_column(default=0)
    last_branch:Mapped[str]=mapped_column(default="main")
    last_package_uid:Mapped[str]=mapped_column(default="")
