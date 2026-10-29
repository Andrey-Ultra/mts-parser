import re

from bs4 import BeautifulSoup
from loguru import logger


class InvalidHtml(Exception):
    pass


def _text(el) -> str:
    """Текст элемента без лишних пробелов (неразрывные пробелы тоже заменяем)."""
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True).replace("\xa0", " ")).strip()


def _to_int(text: str) -> int:
    """'27 890 ₽' -> 27890. Если цифр нет, вернёт 0."""
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


def parse_prices(card) -> dict | None:
    """
    Достаёт цены из карточки. Варианты на сайте:
      1) одна цена без подписи                        -> price
      2) «В комплекте» + «Без комплекта»             -> bundle_price + price
      3) к любому варианту может добавляться зачёркнутая старая цена -> old_price

    price         — цена (или «Без комплекта», если есть комплект)
    bundle_price  — «В комплекте», 0 если такой цены нет
    old_price     — зачёркнутая цена, 0 если скидки нет

    Если ни одной цены нет, возвращает None.
    """
    group = card.select_one(".product-card__price-group")
    if group is None:
        return None

    plain = no_bundle = bundle = old = 0

    for block in group.select("div.price"):
        value_el = block.select_one(".price__value")
        if value_el is None:
            continue
        value = _to_int(_text(value_el))
        if not value:
            continue
        label = _text(block.select_one(".price__label"))

        if "price__value--discount" in value_el.get("class", []):
            old = value
        elif label == "В комплекте":
            bundle = value
        elif label == "Без комплекта":
            no_bundle = value
        else:
            plain = value

    price = no_bundle or plain or bundle
    if not price:
        return None

    return {"price": price, "bundle_price": bundle, "old_price": old}


def get_product_data(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")

    cards = soup.select("div.product-card")
    if not cards:
        raise InvalidHtml("Не найдено ни одной карточки товара: каталог не загрузился")

    products: dict[int, dict] = {}
    seen: set[int] = set()
    out_of_stock = duplicates = no_id = no_name = 0
    broken = []

    for card in cards:
        # ========== Артикул ==========
        # у обёртки карточки id вида "1210998-smartfon-xiaomi-redmi-note-17-pro-..."
        wrapper = card.find_parent(id=True)
        m = re.match(r"(\d+)", wrapper["id"]) if wrapper else None
        if not m:
            no_id += 1
            logger.debug(f"Карточка без id пропущена: {_text(card)[:80]!r}")
            continue
        product_id = int(m.group(1))

        # при подгрузке «Показать ещё» сортировка может сдвигаться, и товар встречается дважды
        if product_id in seen:
            duplicates += 1
            logger.debug(f"Дубль карточки {product_id}")
            continue
        seen.add(product_id)

        # ========== Название ==========
        name_el = card.select_one("a.card-name")
        name = _text(name_el)
        if not name:
            no_name += 1
            logger.debug(f"Карточка {product_id} без названия пропущена")
            continue

        # ========== Наличие ==========
        # нет в наличии: вместо кнопки «Купить» стоит статус, цен в карточке нет
        has_status = card.select_one(".product-card-buy-button__status") is not None
        has_button = card.select_one(".product-card-buy-button button") is not None
        if has_status or not has_button:
            out_of_stock += 1
            continue

        # ========== Цены ==========
        prices = parse_prices(card)
        if prices is None:
            broken.append(product_id)   # кнопка «Купить» есть, а цены нет: не догрузилось
            continue

        url = name_el.get("href", "")
        if url.startswith("/"):
            url = "https://shop.mts.ru" + url

        products[product_id] = {
            "product_id": product_id,
            "name": name,
            **prices,
            "url": url,
        }

    logger.info(
        f"Карточек: {len(cards)} | в наличии: {len(products)} | нет в наличии (пропущено): {out_of_stock} "
        f"| дублей: {duplicates} | без id: {no_id} | без названия: {no_name} | без цены: {len(broken)}"
    )

    # товар в наличии, но без цены: страница не догрузилась, такой результат отдавать нельзя
    if broken:
        raise InvalidHtml(
            f"Нет цены у {len(broken)} товаров в наличии (например {broken[0]}): страница не догрузилась"
        )

    return list(products.values())
