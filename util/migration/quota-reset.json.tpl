{
  "using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"],
  "methodCalls": [
    ["x:Task/set", {
      "accountId": "__ACCOUNT_ID__",
      "create": {
        "quota-reset": {
          "@type": "StoreMaintenance",
          "maintenanceType": "resetUserQuotas",
          "status": {"@type": "Pending", "createdAt": "__NOW__", "due": "__NOW__"}
        }
      }
    }, "0"]
  ]
}
