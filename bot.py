def handle_message(update: Update, context: CallbackContext):
    try:
        if "remove_list" in context.user_data:
            handle_remove_number(update, context)
            return

        user_id = update.effective_user.id
        db.add_user(user_id, update.effective_chat.id)

        asin = AmazonScraper.extract_asin(update.message.text)
        if not asin:
            update.message.reply_text("❌ Invalid Amazon link")
            return

        update.message.reply_text(f"🔍 Fetching {asin}...")

        info = AmazonScraper.fetch_product_info(asin)
        db.add_product(user_id, asin, info["title"], info["url"])

        # 🔥 FIXED: Plain text - no markdown
        status_text = "IN STOCK" if info["status"] == "IN_STOCK" else "OUT OF STOCK"
        update.message.reply_text(
            f"✅ Product Added\n\n"
            f"{info['title'][:200]}\n\n"
            f"Status: {status_text}"
        )
    except Exception as e:
        logger.error(f"Message error: {e}")
        update.message.reply_text("❌ Error adding product")
