from typing import Literal
from pydantic import BaseModel,Field,model_validator
class WindowOutput(BaseModel):
    id:int
    study_id:int
    origin_uid:str
    code:str
    split:Literal['train','cal','test']
    values:dict
    note:str
    revision:int
    last_branch:str
    last_package_uid:str
class WindowAnnotation(BaseModel):
    """A branch-local edit of one window.

    Values, split and note may each be changed independently; anything
    omitted keeps its current value.  The window code is source identity and
    is deliberately not editable, so a branch cannot rename a record and
    thereby dodge three-way matching.
    """
    note:str|None=Field(default=None,max_length=1500)
    split:Literal['train','cal','test']|None=None
    values:dict|None=None
    @model_validator(mode='after')
    def at_least_one(self):
        if self.note is None and self.split is None and self.values is None:
            raise ValueError("至少提交批注、角色或数值中的一项")
        return self
