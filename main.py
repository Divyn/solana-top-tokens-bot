import asyncio
import json
import logging
import aiohttp
import os
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import RetryAfter
from html import escape
from datetime import datetime, timedelta, timezone

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Your bot token from the BotFather
BOT_TOKEN = os.getenv('BOT_TOKEN')

# OAuth Token from https://ide.bitquery.io/ 
OAUTH_TOKEN = os.getenv('OAUTH_TOKEN')

# Function to split long text into smaller parts
def split_text(text, max_length):
    return [text[i:i + max_length] for i in range(0, len(text), max_length)]

# Function to send a long message as multiple smaller messages
async def send_long_message(update: Update, context: ContextTypes.DEFAULT_TYPE, long_message, max_message_length=4000):
    message_parts = split_text(long_message, max_message_length)
    for part in message_parts:
        while True:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id, 
                    text=part,
                    parse_mode=ParseMode.HTML
                )
                break  # Break the loop if the message is sent successfully
            except RetryAfter as e:
                logging.warning(f"Flood control exceeded. Retrying in {e.retry_after} seconds.")
                await asyncio.sleep(e.retry_after)  # Wait for the specified time before retrying

# Function to send the query and process the response
async def send_query_and_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = 'https://streaming.bitquery.io/eap'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {OAUTH_TOKEN}'
    }
    query = """
    query ($time_1h_ago: DateTime) {
      Solana {
        DEXTradeByTokens(
          where: {Transaction: {Result: {Success: true}}, Block: {Time: {after: $time_1h_ago}}, any: [{Trade: {Side: {Currency: {MintAddress: {is: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}}}}}, {Trade: {Currency: {MintAddress: {not: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}}, Side: {Currency: {MintAddress: {is: "So11111111111111111111111111111111111111112"}}}}}, {Trade: {Currency: {MintAddress: {notIn: ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]}}, Side: {Currency: {MintAddress: {notIn: ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]}}}}}]}
          orderBy: {descendingByField: "usd"}
          limit: {count: 10}
        ) {
          Trade {
            Currency {
              Symbol
              Name
              MintAddress
            }
            Side {
              Currency {
                Symbol
                Name
                MintAddress
              }
            }
            price_last: PriceInUSD(maximum: Block_Slot)
            price_1h_ago: PriceInUSD(minimum: Block_Slot)
          }
          dexes: uniq(of: Trade_Dex_ProgramAddress)
          amount: sum(of: Trade_Side_Amount)
          usd: sum(of: Trade_Side_AmountInUSD)
          traders: uniq(of: Trade_Account_Owner)
          count(selectWhere: {ge: "100"})
        }
      }
    }
    """
    now = datetime.now(timezone.utc)
    time_1h_ago = now - timedelta(hours=1)
    
    variables = {
        "time_1h_ago": time_1h_ago.isoformat()
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json={'query': query, 'variables': variables}) as response:
            response_text = await response.text()
            response_json = json.loads(response_text)
            solana_data = response_json.get('data', {}).get('Solana', {}).get('DEXTradeByTokens', [])
            formatted_message = format_message(solana_data)
            await send_long_message(update, context, formatted_message)

def format_message(data):
    message = ""
    for item in data:
        try:
            # Escape dynamic content to prevent issues with HTML parsing
            symbol = escape(item['Trade']['Currency'].get('Symbol', 'N/A'))
            side_symbol = escape(item['Trade']['Side']['Currency'].get('Symbol', 'N/A'))
            mint_address = escape(item['Trade']['Currency'].get('MintAddress', 'N/A'))
            side_mint_address = escape(item['Trade']['Side']['Currency'].get('MintAddress', 'N/A'))
            usd = escape(str(item.get('usd', 'N/A')))
            amount = escape(str(item.get('amount', 'N/A')))
            count = escape(str(item.get('count', 'N/A')))

            message_part = (
                f"<b>{symbol} | {side_symbol} </b>\n"
                f"<code>Address: {mint_address}</code>\n"
                f"🪙 <b>Token Amount:</b> {amount} {symbol}\n"
                f"💰 <b>Trade Amount USD:</b> {usd} USD\n"
                f"👥 <b>Count of Trades:</b> {count}\n\n"
                f"🔗 <a href='https://dexrabbit.com/solana/pair/{mint_address}/{side_mint_address}'>Trade Now</a>\n\n"
            )

            # Check if adding this message part will exceed Telegram's message length limit
            if (len(message) + len(message_part)) > 4096:
                logging.warning("Message length exceeded, consider sending the message in parts.")
                break  # Stop adding more content to avoid exceeding limits

            message += message_part
            
        except Exception as e:
            logging.error(f"Error formatting message for item: {item}. Error: {str(e)}")
            continue  # Skip this item if there's an error in formatting
    
    # Ensure all tags are closed properly
    if message.count('<code>') != message.count('</code>'):
        logging.error("Mismatched <code> tags detected")
        message = message.replace('<code>', '').replace('</code>', '')  # Quick fix by removing code tags

    return message


# Add a global flag to prevent multiple tasks from running
is_task_running = False

async def start_regular_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_task_running
    if is_task_running:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Task already running.")
        return

    is_task_running = True  # Set the flag to indicate that the task is running
    try:
        while True:
            await send_query_and_process(update, context)
            await asyncio.sleep(1800)  # Wait for 30 minutes before sending the next request
    finally:
        is_task_running = False  # Ensure the flag is reset if the loop ends for any reason

# Command handler to start the regular requests
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Starting regular requests every 4 minutes...")
    asyncio.create_task(start_regular_requests(update, context))


# Main function to set up the Telegram bot
if __name__ == '__main__':
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    start_handler = CommandHandler('start', start)
    application.add_handler(start_handler)

    application.run_polling()
