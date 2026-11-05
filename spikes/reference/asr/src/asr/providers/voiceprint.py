"""康伴声纹验证 Provider：基于 SherpaVoiceprintProvider 封装（兼容层）"""

from .voiceprint.sherpa_gate import SherpaVoiceprintProvider

# 兼容旧名称
KangbanVoiceprintProvider = SherpaVoiceprintProvider
