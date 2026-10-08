from fastapi.responses import JSONResponse
from pydantic import BaseModel

class BaseController:
    # Set Json response
    @staticmethod
    def send_response(data, status):
        message = {
            'data': data,
            'status': status
        }
        return message

    # Set success response.
    def success(data, message=None, code=200):
        """form final respose format

        Args:
            data (dict/list): for single record it would be dictionary and for multiple records it would be list.
            message (str): operation response message. Defaults to "".
            code (int): Http response code. Defaults to 200.

        Returns:
            dictionary of response data
            {
                data: [],
                message: "",
                status: 200,
            }
        """
        if isinstance(data, list) and all(isinstance(item, BaseModel) for item in data):
            data = [item.model_dump() for item in data]
        elif isinstance(data, BaseModel):
            data = data.model_dump()
        
        res = {
            "data": data,
            'status': True,
            "statusCode":code,
            "message":message
        }
        return JSONResponse(content=res, status_code=code)
    
    def errorGeneral(error, status_code=403):
        res = {
            'message':error,
            'status': False,
            'statusCode': status_code
        }
        return JSONResponse(content=res, status_code=200)
