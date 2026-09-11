//+------------------------------------------------------------------+
//| TradingBotEA.mq5 — Connects MT5 to the Trading Bot API          |
//| Fetches forex signals via webhook and executes them locally.     |
//+------------------------------------------------------------------+
#property copyright "Trading Bot"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

input string   BotURL         = "https://YOUR-RAILWAY-DOMAIN.up.railway.app";
input string   Secret         = "";          // MT5_SECRET env var value
input int      PollSeconds    = 60;          // How often to check for signals
input double   DefaultLot     = 0.01;
input int      Slippage       = 20;          // points
input int      MagicNumber    = 202509;

CTrade trade;
datetime lastPoll = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(Slippage);
   Print("TradingBotEA initialized — polling ", BotURL, " every ", PollSeconds, "s");
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnTick()
{
   if(TimeCurrent() - lastPoll < PollSeconds) return;
   lastPoll = TimeCurrent();
   PollSignals();
}

//+------------------------------------------------------------------+
void PollSignals()
{
   string url = BotURL + "/mt5/webhook";
   string headers = "Content-Type: application/json\r\n";
   string body = "{\"secret\":\"" + Secret + "\",\"action\":\"get_signals\"}";

   char   post[];
   char   result[];
   string resultHeaders;

   StringToCharArray(body, post, 0, WHOLE_ARRAY, CP_UTF8);

   int res = WebRequest("POST", url, headers, 5000, post, result, resultHeaders);
   if(res != 200)
   {
      Print("WebRequest failed, HTTP ", res);
      return;
   }

   string json = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   ProcessSignals(json);
}

//+------------------------------------------------------------------+
void ProcessSignals(string &json)
{
   // Simple JSON array parser for the signals list
   // Looks for each signal block between { }
   int searchFrom = 0;
   while(true)
   {
      int start = StringFind(json, "{\"id\":", searchFrom);
      if(start < 0) break;
      int end = StringFind(json, "}", start);
      if(end < 0) break;

      string block = StringSubstr(json, start, end - start + 1);
      searchFrom = end + 1;

      string id        = ExtractString(block, "id");
      string pair      = ExtractString(block, "pair");
      string direction = ExtractString(block, "direction");
      double entry     = ExtractDouble(block, "entry_price");
      double sl        = ExtractDouble(block, "sl");
      double tp        = ExtractDouble(block, "tp");
      double lot       = ExtractDouble(block, "lot_size");
      if(lot <= 0) lot = DefaultLot;

      // Map pair name to MT5 symbol (e.g. EUR/USD -> EURUSD)
      string symbol = pair;
      StringReplace(symbol, "/", "");

      if(!SymbolInfoInteger(symbol, SYMBOL_EXIST))
      {
         Print("Symbol not found: ", symbol);
         continue;
      }

      bool ok = false;
      if(direction == "BUY")
         ok = trade.Buy(lot, symbol, 0, sl, tp, "Bot signal " + id);
      else if(direction == "SELL")
         ok = trade.Sell(lot, symbol, 0, sl, tp, "Bot signal " + id);

      if(ok)
      {
         double fillPrice = trade.ResultPrice();
         Print("Executed ", direction, " ", symbol, " @ ", fillPrice);
         ConfirmTrade(id, fillPrice);
      }
      else
      {
         Print("Trade failed for ", symbol, ": ", trade.ResultRetcodeDescription());
      }
   }
}

//+------------------------------------------------------------------+
void ConfirmTrade(string tradeId, double fillPrice)
{
   string url = BotURL + "/mt5/webhook";
   string headers = "Content-Type: application/json\r\n";
   string body = "{\"secret\":\"" + Secret
      + "\",\"action\":\"confirm_trade\",\"trade_id\":\"" + tradeId
      + "\",\"fill_price\":" + DoubleToString(fillPrice, 5) + "}";

   char   post[];
   char   result[];
   string resultHeaders;

   StringToCharArray(body, post, 0, WHOLE_ARRAY, CP_UTF8);
   int res = WebRequest("POST", url, headers, 5000, post, result, resultHeaders);
   if(res == 200)
      Print("Trade confirmed with bot: ", tradeId);
   else
      Print("Confirm failed, HTTP ", res);
}

//+------------------------------------------------------------------+
string ExtractString(string &json, string key)
{
   string search = "\"" + key + "\":\"";
   int start = StringFind(json, search);
   if(start < 0) return "";
   start += StringLen(search);
   int end = StringFind(json, "\"", start);
   if(end < 0) return "";
   return StringSubstr(json, start, end - start);
}

//+------------------------------------------------------------------+
double ExtractDouble(string &json, string key)
{
   string search = "\"" + key + "\":";
   int start = StringFind(json, search);
   if(start < 0) return 0;
   start += StringLen(search);
   // Skip whitespace and possible quote
   string rest = StringSubstr(json, start, 30);
   StringTrimLeft(rest);
   // Find end at comma, brace, or bracket
   int endPos = 0;
   for(int i = 0; i < StringLen(rest); i++)
   {
      ushort c = StringGetCharacter(rest, i);
      if(c == ',' || c == '}' || c == ']' || c == ' ')
      {
         endPos = i;
         break;
      }
      endPos = i + 1;
   }
   return StringToDouble(StringSubstr(rest, 0, endPos));
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("TradingBotEA stopped, reason: ", reason);
}
