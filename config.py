# config.py
# Set the fixed shop name (must match a shop document in Firestore)
SHOP_NAME = "tea_shop_1"

def products_collection():
    return f"{SHOP_NAME}_products"

def sales_collection():
    return f"{SHOP_NAME}_sales"

def stocks_collection():
    return f"{SHOP_NAME}_stocks"

def employees_collection():
    return f"{SHOP_NAME}_employees"

def attendance_collection():
    return f"{SHOP_NAME}_attendance"

def expenses_collection():
    return f"{SHOP_NAME}_expenses"

def vendors_collection():
    return f"{SHOP_NAME}_vendors"

def master_collection(name: str):
    # fallback generic master name
    return f"{SHOP_NAME}_{name}_master"
