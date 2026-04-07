import pandas as pd
import numpy as np


def score_products(
    products: list,
    w_rating: float,
    w_sales: float,
    w_reviews: float,
    w_price: float,
) -> list:
    """
    Score and rank a list of product dicts.
    Weights should be fractions that sum to 1.0 (e.g. 0.20, 0.25, 0.15, 0.40).
    Returns a list of dicts sorted by Final_Score descending.
    """
    if not products:
        return []

    df = pd.DataFrame(products)

    # --- 1. Clean and compute total landed cost ---
    median_shipping = df["Shipping_RWF"].median()
    df["Shipping_RWF"] = df["Shipping_RWF"].fillna(median_shipping)
    df["Total_Cost_RWF"] = df["Price"] + df["Shipping_RWF"]

    df["Rating"] = df["Rating"].fillna(0)
    df["Review_Count"] = df["Review_Count"].fillna(0)

    # --- 2. Normalize metrics to 0–1 scale ---
    # Log1p prevents one viral item from breaking the curve for everything else
    df["Log_Sales"] = np.log1p(df["Sales"])
    df["Log_Reviews"] = np.log1p(df["Review_Count"])

    def min_max_scale(series):
        return (series - series.min()) / (series.max() - series.min() + 1e-9)

    norm_rating = min_max_scale(df["Rating"])
    norm_sales = min_max_scale(df["Log_Sales"])
    norm_reviews = min_max_scale(df["Log_Reviews"])
    # Invert price: cheapest gets 1.0, most expensive gets 0.0
    norm_price = 1 - min_max_scale(df["Total_Cost_RWF"])

    # --- 3. Weighted score ---
    df["Raw_Score"] = (
        (norm_rating * w_rating)
        + (norm_sales * w_sales)
        + (norm_reviews * w_reviews)
        + (norm_price * w_price)
    )
    df["Final_Score"] = (df["Raw_Score"] * 100).round(1)

    # --- 4. Sort and return ---
    df = df.sort_values(by="Final_Score", ascending=False)
    df = df.drop(columns=["Log_Sales", "Log_Reviews", "Raw_Score"], errors="ignore")

    return df.to_dict(orient="records")


# ---------------------------------------------------------------------------
# Standalone entry point (for testing without the web app)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import pandas as pd

    try:
        df = pd.read_csv("scored_products.csv")
        products = df.to_dict(orient="records")
    except FileNotFoundError:
        print("scored_products.csv not found. Run deep_dive.py first.")
        exit(1)

    results = score_products(products, w_rating=0.20, w_sales=0.25, w_reviews=0.15, w_price=0.40)
    out = pd.DataFrame(results)
    out.to_excel("Top_AliExpress_Picks.xlsx", index=False)
    print(f"Saved {len(results)} ranked products to Top_AliExpress_Picks.xlsx")
