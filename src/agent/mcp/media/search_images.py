from src.core.engine import dump


def build(app, owner):
    async def search_images(query: str = "", category: str = "", tag: str = "", source: str = "",
                            since: int = 0) -> str:
        """搜索已收藏图片。query 匹配名称、标签和描述。source 可填 private、group、post 或 comment。since 是保存时间的 Unix 秒。"""
        return dump(app.images.search(owner, query, category, tag, source, since, limit=15))

    return search_images
