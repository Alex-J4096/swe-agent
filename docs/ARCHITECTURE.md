
### provider.py
集成模型供应商。
初始化一个provier类，后续可以从中拿取到供应商的`base_url`和所提供的`model` name.

### tools
开发一个新工具基本分 4 步：
- 定义参数模型 `XxxArgs(BaseModel)`
- 定义工具类 `XxxTool(BaseTool[XxxArgs])`
- 在工具类里绑定 `name / description / args_model`
- 在 `ToolSet` 里注册这个工具

