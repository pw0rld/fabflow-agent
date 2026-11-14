"""Print non-secret SDK contracts on the configured remote build server."""
import dataclasses
from importlib.metadata import version
import inspect
import json
import platform


def main():
    if platform.system() != 'Linux':
        raise SystemExit('Run SDK inspection on the remote build server only.')
    import agents
    from agents import Model, FunctionTool, RunResultStreaming, ModelSettings
    from agents.tool import ToolOutputImage
    result = {'sdk': version('openai-agents'), 'openai': version('openai'),
        'model_get': str(inspect.signature(Model.get_response)),
        'model_stream': str(inspect.signature(Model.stream_response)),
        'stream_cancel': str(inspect.signature(RunResultStreaming.cancel)),
        'tool_dataclass': dataclasses.is_dataclass(FunctionTool),
        'tool_fields': list(FunctionTool.__dataclass_fields__),
        'image_signature': str(inspect.signature(ToolOutputImage)),
        'image_fields': {key: str(value.annotation) for key, value in ToolOutputImage.model_fields.items()},
        'settings_fields': list(ModelSettings.__dataclass_fields__),
        'exports': [key for key in ('Agent', 'Runner', 'OpenAIResponsesModel', 'OpenAIChatCompletionsModel',
                                    'RunConfig', 'function_tool') if hasattr(agents, key)]}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
