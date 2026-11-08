//+------------------------------------------------------------------+
//| ExportCalendar.mq5                                               |
//| TradeQuery: export the MT5 economic calendar to a tab-separated  |
//| file in MQL5\Files, for the news layer.                          |
//|                                                                  |
//| Columns: time_server, currency, importance, event_id, value_id,  |
//|          name, actual, forecast, previous                        |
//| Times are broker server time, like everything else from MT5;     |
//| TradeQuery converts them to UTC on import.                       |
//|                                                                  |
//| The date range comes from the inputs, or from the first line of  |
//| MQL5\Files\tq_calendar_request.txt ("YYYY.MM.DD YYYY.MM.DD")     |
//| when that file exists (used by the automated export).            |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

input datetime InpFrom = D'2026.01.01 00:00';  // first day to export (server time)
input datetime InpTo   = 0;                    // last moment (0 = now)
input string   InpFile = "tq_calendar.csv";    // output file in MQL5\Files

string ImportanceName(ENUM_CALENDAR_EVENT_IMPORTANCE value)
  {
   switch(value)
     {
      case CALENDAR_IMPORTANCE_LOW:      return "low";
      case CALENDAR_IMPORTANCE_MODERATE: return "moderate";
      case CALENDAR_IMPORTANCE_HIGH:     return "high";
     }
   return "none";
  }

string Number(bool present, double value)
  {
   return present ? DoubleToString(value, 6) : "";
  }

string Clean(string text)
  {
   StringReplace(text, "\t", " ");
   StringReplace(text, "\r", " ");
   StringReplace(text, "\n", " ");
   return text;
  }

void OnStart()
  {
   datetime from = InpFrom;
   datetime to = (InpTo == 0) ? TimeTradeServer() : InpTo;

   // Optional request file written by the automated export: "YYYY.MM.DD YYYY.MM.DD"
   int request = FileOpen("tq_calendar_request.txt", FILE_READ | FILE_TXT | FILE_ANSI);
   if(request != INVALID_HANDLE)
     {
      string line = FileReadString(request);
      FileClose(request);
      string parts[];
      if(StringSplit(line, ' ', parts) >= 2)
        {
         from = StringToTime(parts[0]);
         to = StringToTime(parts[1]) + 86399;
        }
     }

   // Right after start-up the terminal may still be downloading the calendar:
   // read until the count is the same twice in a row (up to about two minutes).
   MqlCalendarValue values[];
   int count = -1, previous = -2;
   for(int attempt = 0; attempt < 24 && !IsStopped(); attempt++)
     {
      ResetLastError();
      count = CalendarValueHistory(values, from, to, NULL, NULL);
      if(count > 0 && count == previous)
         break;
      previous = count;
      Sleep(5000);
     }
   if(count < 0)
     {
      PrintFormat("TradeQuery calendar export failed: error %d", GetLastError());
      return;
     }

   int out = FileOpen(InpFile + ".part", FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(out == INVALID_HANDLE)
     {
      PrintFormat("TradeQuery calendar export: cannot write %s (error %d)", InpFile, GetLastError());
      return;
     }
   FileWriteString(out, "time_server\tcurrency\timportance\tevent_id\tvalue_id\tname\tactual\tforecast\tprevious\r\n");
   int written = 0;
   for(int i = 0; i < count; i++)
     {
      MqlCalendarEvent event;
      MqlCalendarCountry country;
      if(!CalendarEventById(values[i].event_id, event) || !CalendarCountryById(event.country_id, country))
         continue;
      string row = TimeToString(values[i].time, TIME_DATE | TIME_SECONDS) + "\t" +
                   country.currency + "\t" +
                   ImportanceName(event.importance) + "\t" +
                   IntegerToString((long)event.id) + "\t" +
                   IntegerToString((long)values[i].id) + "\t" +
                   Clean(event.name) + "\t" +
                   Number(values[i].HasActualValue(), values[i].GetActualValue()) + "\t" +
                   Number(values[i].HasForecastValue(), values[i].GetForecastValue()) + "\t" +
                   Number(values[i].HasPreviousValue(), values[i].GetPreviousValue()) + "\r\n";
      FileWriteString(out, row);
      written++;
     }
   FileClose(out);
   // Rename at the end, so a reader never sees a half-written file.
   FileDelete(InpFile);
   FileMove(InpFile + ".part", 0, InpFile, FILE_REWRITE);
   PrintFormat("TradeQuery calendar export: %d events from %s to %s written to MQL5\\Files\\%s",
               written, TimeToString(from), TimeToString(to), InpFile);
  }
//+------------------------------------------------------------------+
