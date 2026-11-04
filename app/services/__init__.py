"""领域服务层：**唯一写库层**（技术文档 §1.3）。

api / audio / scheduler / graphs 一律通过这里读写数据；任何模块不得越过本层直接操作 ORM。
按域拆分：medication / health / memory / workorder / report / elder / notify / persona。
"""
