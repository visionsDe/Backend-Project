from pydantic import BaseModel, ConfigDict
from typing import Optional, Any


class PaginationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total:      Optional[int] = None
    page:       Optional[int] = None
    skip:       Optional[int] = None
    per_page:   Optional[int] = None
    data:       Any
    statusCode: Optional[int] = 200
    status: Optional[bool] = True
    message: Optional[str] = None